"""
Phase 3: Context Retrieval API.

Exposes two endpoints:
  GET  /health          -- liveness check
  POST /similar-events  -- given a log event, return top-k semantically similar past incidents
  POST /runbooks        -- given an alert description, return top-k relevant runbooks

Both endpoints encode the query with the same sentence transformer used during
ingestion, then run a kNN search against the corresponding ES index. Because the
query and the stored documents are in the same 384-dimensional vector space, the
search finds semantically similar results even when there is zero keyword overlap.

WHY kNN HERE AND NOT BM25:
    The queries to this service are free-form descriptions or raw event objects,
    not structured field filters. "find events similar to this kernel event" has
    no single field value to term-match on — it requires semantic understanding
    of the event as a whole. kNN vector search is the right primitive for this.
    The /search endpoint (port 8000) remains BM25 for structured field queries.

HOW TO RUN (from project root):
    .venv\\Scripts\\python -m uvicorn retrieve:app --app-dir services/context-retrieval --host 0.0.0.0 --port 8002
"""

import logging
import os
import sys
from typing import Any

from elasticsearch import Elasticsearch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))
from config import ELASTIC_URL

# ---------------------------------------------------------------------------
# Module-level resource loading — once at startup, never per request.
# The sentence transformer (~90MB) and the ES client are expensive to create.
# ---------------------------------------------------------------------------

_ES_URL = ELASTIC_URL
_VECTOR_INDEX = "log-event-vectors"
_RUNBOOK_INDEX = "runbook-vectors"

logger.info("Loading sentence transformer (all-MiniLM-L6-v2)...")
_model = SentenceTransformer("all-MiniLM-L6-v2")
logger.info("Model loaded.")

_es = Elasticsearch(_ES_URL)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Context Retrieval API", version="1.0.0")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class EventQuery(BaseModel):
    """
    A single log event to use as the similarity query.
    Mirrors the LogEvent schema in predict.py so callers can pipe
    the same payload to both /predict and /similar-events.
    """
    log_type: str = Field(..., examples=["deep_kernel", "dns", "standard_host"])
    attributes: dict[str, Any] = Field(default_factory=dict)
    k: int = Field(5, ge=1, le=20, description="Number of similar events to return")


class TextQuery(BaseModel):
    """Free-form text query for runbook search."""
    query: str = Field(..., min_length=3, max_length=1000)
    k: int = Field(3, ge=1, le=10, description="Number of runbooks to return")


class SimilarEvent(BaseModel):
    id: str
    score: float
    log_type: str
    log_attribute: str
    timestamp: str
    text: str


class Runbook(BaseModel):
    id: str
    score: float
    title: str
    severity: str
    tags: list[str]
    content: str


class SimilarEventsResponse(BaseModel):
    query_text: str
    hits: list[SimilarEvent]


class RunbooksResponse(BaseModel):
    query: str
    hits: list[Runbook]


# ---------------------------------------------------------------------------
# Text builder — same logic as embed.py so query and doc are in the same space
# ---------------------------------------------------------------------------

def _event_to_text(log_type: str, attrs: dict) -> str:
    """
    Convert an incoming event into the same text format used during ingestion.

    CRITICAL: This must match _doc_to_text() in embed.py exactly. If the
    query text is formatted differently from the stored document text, the
    embeddings land in different regions of the vector space and similarity
    scores become meaningless. Any change here must be mirrored in embed.py.
    """
    parts = [f"log type: {log_type}"]

    if attrs.get("process_name"):
        parts.append(f"process: {attrs['process_name']}")

    uid = attrs.get("user_id")
    if uid is not None:
        parts.append(f"user id: {uid}" + (" (root)" if str(uid) == "0" else ""))

    if attrs.get("args"):
        parts.append(f"args: {str(attrs['args'])[:200]}")

    if attrs.get("dns_query"):
        parts.append(f"dns query: {attrs['dns_query']}")

    rv = attrs.get("return_value")
    if rv is not None:
        try:
            parts.append("syscall failed" if int(rv) < 0 else f"return value: {rv}")
        except (ValueError, TypeError):
            pass

    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Core kNN search
# ---------------------------------------------------------------------------

def _knn_search(index: str, embedding: list[float], k: int, num_candidates: int) -> list[dict]:
    """
    Run a kNN search against an ES dense_vector index.

    num_candidates controls HNSW search breadth. A larger value explores more
    of the graph and is more accurate, at the cost of latency. The rule of thumb
    is num_candidates = 5-10x k. For k=5: num_candidates=50. For k=3: 30.

    WHY num_candidates > k:
    HNSW is approximate — it may miss the true nearest neighbor if the graph
    traversal terminates early. num_candidates tells the HNSW algorithm to
    keep searching until it has visited that many candidate nodes, ensuring
    the top-k returned are very likely the actual top-k.
    """
    try:
        resp = _es.search(
            index=index,
            knn={
                "field":        "embedding",
                "query_vector": embedding,
                "k":            k,
                "num_candidates": num_candidates,
            },
        )
        return resp["hits"]["hits"]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Elasticsearch error: {e}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    es_ok = _es.ping()
    if not es_ok:
        raise HTTPException(status_code=503, detail="Elasticsearch unreachable")
    return {
        "status": "ok",
        "es_url": _ES_URL,
        "vector_index": _VECTOR_INDEX,
        "runbook_index": _RUNBOOK_INDEX,
        "embedding_dim": 384,
    }


@app.post("/similar-events", response_model=SimilarEventsResponse)
def similar_events(req: EventQuery):
    """
    Return the top-k past security events most semantically similar to the query event.

    Use case: when the triage model flags a new event as suspicious or evil,
    the Incident Copilot calls this endpoint to find precedents — past incidents
    that looked like this one and how they were handled.
    """
    query_text = _event_to_text(req.log_type, req.attributes)

    try:
        embedding = _model.encode(query_text, convert_to_numpy=True).tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Encoding error: {e}")

    hits = _knn_search(_VECTOR_INDEX, embedding, k=req.k, num_candidates=req.k * 10)

    results = [
        SimilarEvent(
            id=h["_id"],
            score=round(h["_score"], 4),
            log_type=h["_source"].get("log_type", ""),
            log_attribute=h["_source"].get("log_attribute", ""),
            timestamp=h["_source"].get("timestamp", ""),
            text=h["_source"].get("text", ""),
        )
        for h in hits
    ]

    return SimilarEventsResponse(query_text=query_text, hits=results)


@app.post("/runbooks", response_model=RunbooksResponse)
def runbooks(req: TextQuery):
    """
    Return the top-k runbooks most relevant to the alert description.

    Use case: when an incident is detected, the Incident Copilot calls this
    endpoint to retrieve the most relevant response runbooks to include in
    the grounding context for the LLM. The LLM then generates a recommended
    playbook based on the retrieved runbook content.
    """
    try:
        embedding = _model.encode(req.query, convert_to_numpy=True).tolist()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Encoding error: {e}")

    hits = _knn_search(_RUNBOOK_INDEX, embedding, k=req.k, num_candidates=req.k * 10)

    results = [
        Runbook(
            id=h["_id"],
            score=round(h["_score"], 4),
            title=h["_source"].get("title", ""),
            severity=h["_source"].get("severity", ""),
            tags=h["_source"].get("tags", []),
            content=h["_source"].get("content", ""),
        )
        for h in hits
    ]

    return RunbooksResponse(query=req.query, hits=results)
