"""
Phase 3: Runbook ingestion — embed runbook text files into Elasticsearch.

Reads every .txt file in the runbooks/ directory, encodes each one into a
384-dimensional vector, and writes the result to the 'runbook-vectors' index.

WHY SEPARATE FROM log-event-vectors:
    Events and runbooks serve different retrieval purposes:
    - Similar events answer: "what other attacks looked like this one?"
    - Runbooks answer: "what should the analyst DO about this attack?"
    Separating them into distinct indices lets the retrieve.py service apply
    different k values, different num_candidates, and different result
    post-processing for each type of query — without the two result sets
    interfering with each other.

HOW TO RUN (from project root):
    .venv\\Scripts\\python services/context-retrieval/ingest_runbooks.py
"""

import glob
import logging
import os
import sys

from elasticsearch import Elasticsearch, helpers
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))
from config import ELASTIC_URL

RUNBOOK_DIR = os.path.join(os.path.dirname(__file__), "runbooks")
RUNBOOK_INDEX = "runbook-vectors"
VECTOR_DIM = 384

RUNBOOK_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "filename":  {"type": "keyword"},
            "title":     {"type": "text"},
            "severity":  {"type": "keyword"},
            "tags":      {"type": "keyword"},
            "content":   {"type": "text"},
            "embedding": {
                "type":       "dense_vector",
                "dims":       VECTOR_DIM,
                "index":      True,
                "similarity": "cosine",
            },
        }
    }
}


def _parse_runbook(path: str) -> dict:
    """
    Extract metadata from a runbook's header lines before embedding.

    Runbooks follow a loose convention:
        RUNBOOK: <title>
        Severity: <level>
        Tags: <comma-separated>

    If the file doesn't follow this format, we fall back to using the
    filename as the title and leaving severity/tags empty. The full
    content is always passed to the encoder regardless.
    """
    with open(path, encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    metadata = {"title": os.path.basename(path), "severity": "", "tags": []}

    for line in lines[:5]:
        if line.startswith("RUNBOOK:"):
            metadata["title"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("severity:"):
            metadata["severity"] = line.split(":", 1)[1].strip().lower()
        elif line.lower().startswith("tags:"):
            raw_tags = line.split(":", 1)[1].strip()
            metadata["tags"] = [t.strip() for t in raw_tags.split(",")]

    metadata["content"] = content
    return metadata


def run(es: Elasticsearch, model: SentenceTransformer) -> None:
    if not es.indices.exists(index=RUNBOOK_INDEX):
        es.indices.create(index=RUNBOOK_INDEX, body=RUNBOOK_INDEX_MAPPING)
        logger.info(f"Created index '{RUNBOOK_INDEX}'.")

    paths = sorted(glob.glob(os.path.join(RUNBOOK_DIR, "*.txt")))
    if not paths:
        logger.warning(f"No .txt files found in {RUNBOOK_DIR}.")
        return

    logger.info(f"Embedding {len(paths)} runbooks...")

    runbooks = [_parse_runbook(p) for p in paths]
    texts = [rb["content"] for rb in runbooks]

    # Encode all runbooks in one batch — there are only ~10 of them, so
    # a single encode() call is fine. show_progress_bar=True is useful
    # for longer documents where encoding takes a few seconds.
    vectors = model.encode(texts, show_progress_bar=True, convert_to_numpy=True)

    actions = []
    for path, rb, vec in zip(paths, runbooks, vectors):
        doc_id = os.path.splitext(os.path.basename(path))[0]
        actions.append({
            "_index": RUNBOOK_INDEX,
            "_id":    doc_id,
            "_source": {
                "filename":  os.path.basename(path),
                "title":     rb["title"],
                "severity":  rb["severity"],
                "tags":      rb["tags"],
                "content":   rb["content"],
                "embedding": vec.tolist(),
            },
        })

    success, errors = helpers.bulk(es, actions, raise_on_error=False)
    if errors:
        logger.error(f"Bulk write errors: {errors}")
    logger.info(f"Wrote {success} runbooks to '{RUNBOOK_INDEX}'.")


if __name__ == "__main__":
    es = Elasticsearch(ELASTIC_URL)
    if not es.ping():
        logger.error(f"Elasticsearch not reachable at {ELASTIC_URL}. Is Docker running?")
        sys.exit(1)

    logger.info("Loading sentence transformer (all-MiniLM-L6-v2)...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    run(es, model)
