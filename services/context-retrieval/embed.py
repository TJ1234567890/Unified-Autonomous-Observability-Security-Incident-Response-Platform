"""
Phase 3: Batch embedding job for security events.

Scrolls all suspicious/evil events from the main ES index, encodes each one
into a 384-dimensional vector using sentence-transformers, and writes the
result to a separate 'log-event-vectors' index.

WHY A SEPARATE INDEX:
    Keeping embeddings in a dedicated index avoids bloating the main
    'beth-security-logs' index with 384 floats per document. The main index
    stays lean for BM25 keyword search. The vector index has a minimal schema
    (id + embedding + a few metadata fields) optimized for kNN.

WHY SCROLL AND NOT PAGINATED SEARCH:
    Standard ES pagination (from + size) is O(n) for deep pages — it scores
    and sorts all prior documents before skipping them. The Scroll API creates
    a point-in-time snapshot and returns pages with a cursor token, O(1) per
    page regardless of position. Required for processing millions of documents.

HOW TO RUN (from project root):
    .venv\\Scripts\\python services/context-retrieval/embed.py
"""

import os
import sys
import logging

from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))
from config import ELASTIC_URL

SOURCE_INDEX = "beth-security-logs"
VECTOR_INDEX = "log-event-vectors"

# Only embed events the triage model flagged — benign events at this scale
# (3.8M) would be too slow and too expensive to embed all at once. Suspicious
# and evil events are the ones the Incident Copilot needs to reason about.
SOURCE_FILTER = {"bool": {"should": [
    {"term": {"labels.sus": 1}},
    {"term": {"labels.evil": 1}},
]}}

SCROLL_WINDOW = "5m"
SCROLL_BATCH = 256      # documents per scroll page
EMBED_BATCH = 64        # documents per sentence-transformer encode call
VECTOR_DIM = 384        # all-MiniLM-L6-v2 output dimension


# ---------------------------------------------------------------------------
# Index setup
# ---------------------------------------------------------------------------

VECTOR_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "source_id":   {"type": "keyword"},
            "log_type":    {"type": "keyword"},
            "log_attribute": {"type": "keyword"},
            "timestamp":   {"type": "date", "format": "strict_date_optional_time||epoch_millis"},
            "text":        {"type": "text"},
            "embedding": {
                "type":       "dense_vector",
                "dims":       VECTOR_DIM,
                "index":      True,
                "similarity": "cosine",
            },
        }
    }
}


def _ensure_vector_index(es: Elasticsearch) -> None:
    """Create the vector index if it does not exist yet."""
    if not es.indices.exists(index=VECTOR_INDEX):
        es.indices.create(index=VECTOR_INDEX, body=VECTOR_INDEX_MAPPING)
        logger.info(f"Created index '{VECTOR_INDEX}'.")
    else:
        logger.info(f"Index '{VECTOR_INDEX}' already exists — skipping creation.")


# ---------------------------------------------------------------------------
# Text representation for embedding
# ---------------------------------------------------------------------------

def _doc_to_text(doc: dict) -> str:
    """
    Convert an ES document into a single string for the sentence transformer.

    WHY NOT EMBED RAW JSON:
    The sentence transformer was trained on natural language, not JSON syntax.
    Feeding it raw JSON pollutes the embedding with noise tokens like braces,
    quotes, and colons that carry no semantic meaning. Building a natural-
    language summary from the structured fields produces a more meaningful
    embedding — semantically similar events will be closer in vector space.
    """
    src = doc.get("_source", {})
    attrs = src.get("attributes", {})
    feats = src.get("features", {})
    parts = []

    log_type = src.get("log_type", "")
    parts.append(f"log type: {log_type}")

    if src.get("event_name"):
        parts.append(f"event: {src['event_name']}")

    if attrs.get("process_name"):
        parts.append(f"process: {attrs['process_name']}")

    if attrs.get("user_id") is not None:
        uid = attrs["user_id"]
        parts.append(f"user id: {uid}" + (" (root)" if str(uid) == "0" else ""))

    if attrs.get("args"):
        parts.append(f"args: {str(attrs['args'])[:200]}")

    if attrs.get("dns_query"):
        parts.append(f"dns query: {attrs['dns_query']}")

    if attrs.get("return_value") is not None:
        rv = attrs["return_value"]
        try:
            parts.append("syscall failed" if int(rv) < 0 else f"return value: {rv}")
        except (ValueError, TypeError):
            pass

    if feats.get("feat_args_has_shell"):
        parts.append("shell execution detected")
    if feats.get("feat_args_has_network"):
        parts.append("network tool in args")
    if feats.get("feat_args_has_sensitive_path"):
        parts.append("sensitive path accessed")
    if feats.get("feat_dns_query_entropy", 0) > 3.5:
        parts.append("high entropy dns query")

    labels = src.get("labels", {})
    if labels.get("evil"):
        parts.append("confirmed malicious")
    elif labels.get("sus"):
        parts.append("suspicious activity")

    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Embedding loop
# ---------------------------------------------------------------------------

def run(es: Elasticsearch, model: SentenceTransformer) -> None:
    _ensure_vector_index(es)

    resp = es.search(
        index=SOURCE_INDEX,
        body={"query": SOURCE_FILTER},
        scroll=SCROLL_WINDOW,
        size=SCROLL_BATCH,
    )
    scroll_id = resp["_scroll_id"]
    total_embedded = 0

    try:
        while True:
            hits = resp["hits"]["hits"]
            if not hits:
                break

            # Process in sub-batches so the encoder gets a full batch at once.
            for i in range(0, len(hits), EMBED_BATCH):
                batch = hits[i: i + EMBED_BATCH]
                texts = [_doc_to_text(h) for h in batch]

                # encode() returns a numpy array of shape (batch_size, 384).
                # convert_to_numpy=True (default) is faster than convert_to_tensor.
                vectors = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)

                actions = []
                for doc, vec in zip(batch, vectors):
                    src = doc.get("_source", {})
                    actions.append({
                        "_index": VECTOR_INDEX,
                        "_id":    doc["_id"],   # same ID as the source document
                        "_source": {
                            "source_id":     doc["_id"],
                            "log_type":      src.get("log_type", ""),
                            "log_attribute": src.get("log_attribute", ""),
                            "timestamp":     src.get("timestamp", ""),
                            "text":          _doc_to_text(doc),
                            "embedding":     vec.tolist(),
                        },
                    })

                from elasticsearch import helpers
                helpers.bulk(es, actions, raise_on_error=False)
                total_embedded += len(batch)

            logger.info(f"Embedded {total_embedded:,} events so far...")
            resp = es.scroll(scroll_id=scroll_id, scroll=SCROLL_WINDOW)

    finally:
        # Always release the scroll context — it holds a server-side snapshot
        # that consumes heap memory on the ES node until explicitly cleared.
        es.clear_scroll(scroll_id=scroll_id)

    logger.info(f"Done. {total_embedded:,} events written to '{VECTOR_INDEX}'.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    es = Elasticsearch(ELASTIC_URL)
    if not es.ping():
        logger.error(f"Elasticsearch not reachable at {ELASTIC_URL}. Is Docker running?")
        sys.exit(1)

    logger.info("Loading sentence transformer (all-MiniLM-L6-v2)...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    logger.info("Model loaded.")

    run(es, model)
