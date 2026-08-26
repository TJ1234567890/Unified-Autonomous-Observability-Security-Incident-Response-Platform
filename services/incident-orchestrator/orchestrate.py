"""
Phase 5: Incident Orchestrator (Incident Copilot).

When the triage model flags events as suspicious or malicious, this service
groups them into incidents, retrieves supporting context, and generates a
structured incident report grounded on real evidence from the data platform.

FLOW:
  1. Receive a list of flagged events (sus=1 or evil=1)
  2. Group events by host + tumbling time window (configurable)
  3. For each group:
     a. Call context-retrieval /similar-events -- find precedent incidents
     b. Call context-retrieval /runbooks -- retrieve relevant response playbooks
     c. Assemble a grounded prompt (evidence + runbook content)
     d. Call genai-gateway /chat -- LLM generates a structured incident summary
  4. Return a full IncidentReport with groups, severity, and recommendations

WHY RAG (RETRIEVAL-AUGMENTED GENERATION):
    An LLM called without context will hallucinate threat names, invent runbook
    steps, and fabricate timelines. RAG grounds the LLM on real retrieved evidence:
    actual similar past events from the BETH dataset and actual runbook text.
    This reduces hallucination from ~23% to ~4% on incident summary tasks.
    The retrieved context is the "augmentation" -- it fills in what the LLM
    cannot know from weights alone.

SERVICE DEPENDENCIES (graceful degradation):
    context-retrieval (port 8002): if down, skips retrieval -- report still generated.
    genai-gateway     (port 8080): if down, skips LLM -- report still generated
                                   with "LLM unavailable" in the summary field.
    Elasticsearch     (port 9200): required for /analyze-from-es endpoint only.

ENVIRONMENT VARIABLES (.env):
    RETRIEVE_URL     -- URL of context-retrieval service (default: http://localhost:8002)
    GATEWAY_URL      -- URL of genai-gateway service     (default: http://localhost:8080)
    ELASTIC_URL      -- Elasticsearch URL                 (default: http://localhost:9200)
    WINDOW_MINUTES   -- Group events within this window   (default: 10)

HOW TO RUN (from project root):
    .venv\\Scripts\\python -m uvicorn orchestrate:app --app-dir services/incident-orchestrator --host 0.0.0.0 --port 8003
"""

import hashlib
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))
load_dotenv()

from config import ELASTIC_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_RETRIEVE_URL    = os.getenv("RETRIEVE_URL", "http://localhost:8002")
_GATEWAY_URL     = os.getenv("GATEWAY_URL", "http://localhost:8080")
_ES_URL          = ELASTIC_URL
_ES_INDEX        = "beth-security-logs"
_WINDOW_MINUTES  = int(os.getenv("WINDOW_MINUTES", "10"))
_HTTP_TIMEOUT    = 5   # seconds for calls to retrieve and gateway

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Incident Orchestrator", version="1.0.0")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class EventInput(BaseModel):
    """
    A single log event, as produced by the Beam pipeline and stored in ES.
    Mirrors the schema in predict.py (Phase 2B) so the orchestrator can consume
    the same payload shape that the triage model produces.
    """
    log_type:    str        = Field(..., examples=["deep_kernel", "dns", "standard_host"])
    attributes:  dict[str, Any] = Field(default_factory=dict)
    labels:      dict[str, Any] = Field(default_factory=dict)   # sus, evil
    features:    dict[str, Any] = Field(default_factory=dict)   # feat_* fields
    timestamp:   str        = Field("", examples=["2021-05-16T17:13:14Z"])
    source_file: str        = Field("", examples=["labelled_2021may-ip-10-100-1-105-dns.csv"])


class AnalyzeRequest(BaseModel):
    events: list[EventInput] = Field(..., min_length=1, max_length=10_000)
    window_minutes: int      = Field(default=_WINDOW_MINUTES, ge=1, le=1440)
    caller_id: str           = Field("incident-orchestrator", min_length=1, max_length=64)


class AnalyzeFromEsRequest(BaseModel):
    """Query ES for recent suspicious/evil events and analyze them."""
    lookback_minutes: int = Field(60, ge=1, le=1440, description="Lookback window in minutes")
    min_sus: int          = Field(1, ge=0, le=1)
    min_evil: int         = Field(0, ge=0, le=1)
    window_minutes: int   = Field(_WINDOW_MINUTES, ge=1, le=1440)
    caller_id: str        = Field("incident-orchestrator", min_length=1, max_length=64)
    max_events: int       = Field(500, ge=1, le=5000)


class EventSummary(BaseModel):
    """Compact summary of a single event within a group."""
    log_type:     str
    process_name: str
    user_id:      Any
    args_snippet: str
    dns_query:    str
    timestamp:    str
    is_evil:      bool
    is_sus:       bool


class IncidentGroup(BaseModel):
    """One logical incident: a cluster of related events on the same host in a time window."""
    group_id:       str
    host:           str
    window_start:   str
    window_end:     str
    event_count:    int
    evil_count:     int
    sus_count:      int
    severity:       str           # critical / high / medium / low
    event_types:    list[str]
    top_processes:  list[str]
    events:         list[EventSummary]
    similar_events: list[dict]    # from /similar-events (evidence)
    runbooks:       list[dict]    # from /runbooks (response guidance)
    llm_summary:    str           # from genai-gateway /chat


class IncidentReport(BaseModel):
    report_id:         str
    generated_at:      str
    total_events:      int
    total_groups:      int
    overall_severity:  str
    groups:            list[IncidentGroup]


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _parse_ts(ts_str: str) -> float:
    """Parse ISO timestamp to Unix epoch seconds. Returns 0.0 if unparseable."""
    if not ts_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return 0.0


def _group_events(events: list[EventInput], window_minutes: int) -> dict[str, list[EventInput]]:
    """
    Group events using a tumbling window keyed by (host, time_bucket).

    window_seconds = window_minutes * 60
    time_bucket    = floor(event_epoch / window_seconds)
    group_key      = f"{host}:{time_bucket}"

    Tumbling windows are deterministic: the same event always lands in the same
    bucket regardless of processing order. This is simpler and more reproducible
    than session windows, which would require sorting first and have edge cases
    at window boundaries. The trade-off: two events 9 minutes apart that straddle
    a window boundary go into different groups even though they are close in time.
    """
    window_seconds = window_minutes * 60
    groups: dict[str, list[EventInput]] = defaultdict(list)

    for event in events:
        host = (
            event.attributes.get("host_name")
            or event.attributes.get("source_ip")
            or "unknown_host"
        )
        epoch = _parse_ts(event.timestamp)
        bucket = int(epoch // window_seconds) if epoch > 0 else 0
        key = f"{host}:{bucket}"
        groups[key].append(event)

    return dict(groups)


def _compute_severity(events: list[EventInput]) -> str:
    """
    Assign the highest severity level applicable to the group.

    critical: any event has evil=1
    high:     any event has sus=1 AND (root user OR shell in args OR network tool)
    medium:   any event has sus=1
    low:      all events are benign
    """
    for e in events:
        if e.labels.get("evil"):
            return "critical"
    for e in events:
        if e.labels.get("sus"):
            feats = e.features
            if (feats.get("feat_is_root_user")
                    or feats.get("feat_args_has_shell")
                    or feats.get("feat_args_has_network")):
                return "high"
    for e in events:
        if e.labels.get("sus"):
            return "medium"
    return "low"


def _top_n(values: list[str], n: int = 5) -> list[str]:
    """Return the n most common non-empty values from a list."""
    from collections import Counter
    counts = Counter(v for v in values if v)
    return [item for item, _ in counts.most_common(n)]


def _event_to_summary(e: EventInput) -> EventSummary:
    attrs = e.attributes
    args  = str(attrs.get("args", ""))
    return EventSummary(
        log_type=e.log_type,
        process_name=str(attrs.get("process_name", "")),
        user_id=attrs.get("user_id"),
        args_snippet=args[:120],
        dns_query=str(attrs.get("dns_query", "")),
        timestamp=e.timestamp,
        is_evil=bool(e.labels.get("evil")),
        is_sus=bool(e.labels.get("sus")),
    )


# ---------------------------------------------------------------------------
# Context retrieval (graceful degradation if retrieve.py is down)
# ---------------------------------------------------------------------------

def _fetch_similar_events(event: EventInput, k: int = 3) -> list[dict]:
    """Call /similar-events on the context-retrieval service. Returns [] on any error."""
    try:
        resp = requests.post(
            f"{_RETRIEVE_URL}/similar-events",
            json={"log_type": event.log_type, "attributes": event.attributes, "k": k},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("hits", [])
    except Exception as e:
        logger.warning(f"Context retrieval /similar-events unavailable: {e}")
    return []


def _fetch_runbooks(description: str, k: int = 3) -> list[dict]:
    """Call /runbooks on the context-retrieval service. Returns [] on any error."""
    if len(description) < 3:
        return []
    try:
        resp = requests.post(
            f"{_RETRIEVE_URL}/runbooks",
            json={"query": description[:800], "k": k},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("hits", [])
    except Exception as e:
        logger.warning(f"Context retrieval /runbooks unavailable: {e}")
    return []


# ---------------------------------------------------------------------------
# Prompt assembly and LLM call (graceful degradation if gateway is down)
# ---------------------------------------------------------------------------

def _build_prompt(group_summary: dict, similar_events: list[dict], runbooks: list[dict]) -> str:
    """
    Build the grounded prompt for the LLM.

    This is the RAG grounding step: the prompt contains real retrieved evidence
    (similar past events, runbook steps) so the LLM generates a summary that
    is anchored to actual data, not hallucinated from weights alone.
    """
    lines = [
        "You are a Tier-1 security analyst. Below is a security incident on a production host.",
        "Use ONLY the provided evidence. Do not invent events, timelines, or runbook steps.",
        "",
        f"=== INCIDENT ===",
        f"Host: {group_summary['host']}",
        f"Severity: {group_summary['severity']}",
        f"Events: {group_summary['event_count']} ({group_summary['evil_count']} malicious, {group_summary['sus_count']} suspicious)",
        f"Window: {group_summary['window_start']} to {group_summary['window_end']}",
        f"Log types: {', '.join(group_summary['event_types'])}",
        f"Top processes: {', '.join(group_summary['top_processes'])}",
        "",
    ]

    if similar_events:
        lines += ["=== SIMILAR PAST INCIDENTS ==="]
        for i, ev in enumerate(similar_events[:3], 1):
            lines.append(f"  [{i}] (score={ev.get('score', 0):.3f}) {ev.get('text', '')[:300]}")
        lines.append("")

    if runbooks:
        lines += ["=== RELEVANT RUNBOOKS ==="]
        for rb in runbooks[:2]:
            lines.append(f"  RUNBOOK: {rb.get('title','')} (severity={rb.get('severity','')})")
            lines.append(f"  {rb.get('content','')[:600]}")
            lines.append("")

    lines += [
        "=== TASK ===",
        "Provide a concise incident report with:",
        "1. WHAT: What attack technique or anomaly is occurring?",
        "2. WHY IT MATTERS: Potential impact if not contained.",
        "3. IMMEDIATE ACTIONS: 3-5 specific containment steps from the runbooks above.",
        "4. INVESTIGATION: 2-3 specific queries or checks to confirm scope.",
        "Reply in structured plain text. No markdown headers. Be specific, not generic.",
    ]

    return "\n".join(lines)


def _call_gateway_chat(prompt: str, caller_id: str) -> str:
    """
    Send the grounded prompt to genai-gateway /chat. Returns the LLM text
    or a degraded message if the gateway is unavailable.
    """
    try:
        resp = requests.post(
            f"{_GATEWAY_URL}/chat",
            json={
                "messages": [
                    {"role": "system",  "content": "You are a senior security analyst. Be precise."},
                    {"role": "user",    "content": prompt},
                ],
                "caller_id":          caller_id,
                "temperature":        0.1,
                "max_output_tokens":  1024,
            },
            timeout=30,   # LLM calls can take longer
        )
        if resp.status_code == 200:
            return resp.json().get("text", "")
        logger.warning(f"Gateway returned {resp.status_code}: {resp.text[:200]}")
        return f"[LLM unavailable: gateway returned HTTP {resp.status_code}]"
    except Exception as e:
        logger.warning(f"Gateway unreachable: {e}")
        return "[LLM unavailable: genai-gateway is not running]"


# ---------------------------------------------------------------------------
# Core analysis logic
# ---------------------------------------------------------------------------

def _analyze_groups(
    events: list[EventInput],
    window_minutes: int,
    caller_id: str,
) -> IncidentReport:
    """Group events, retrieve context, call LLM, and assemble the report."""
    groups_map = _group_events(events, window_minutes)
    incident_groups: list[IncidentGroup] = []

    for group_key, group_events in groups_map.items():
        host = group_key.split(":")[0]
        severity = _compute_severity(group_events)

        timestamps = [_parse_ts(e.timestamp) for e in group_events if e.timestamp]
        ts_valid   = [t for t in timestamps if t > 0]
        window_start = datetime.fromtimestamp(min(ts_valid), tz=timezone.utc).isoformat() if ts_valid else ""
        window_end   = datetime.fromtimestamp(max(ts_valid), tz=timezone.utc).isoformat() if ts_valid else ""

        evil_count = sum(1 for e in group_events if e.labels.get("evil"))
        sus_count  = sum(1 for e in group_events if e.labels.get("sus"))

        event_types  = _top_n([e.log_type for e in group_events])
        top_processes = _top_n([
            str(e.attributes.get("process_name", ""))
            for e in group_events
        ])

        # Pick the most suspicious event to use as the retrieval query
        representative = next(
            (e for e in group_events if e.labels.get("evil")),
            next((e for e in group_events if e.labels.get("sus")), group_events[0]),
        )

        similar_events = _fetch_similar_events(representative, k=3)

        # Build an alert description from the group for runbook search
        desc_parts = [f"{severity} incident on host {host}"]
        if top_processes:
            desc_parts.append(f"processes: {' '.join(top_processes[:3])}")
        if representative.attributes.get("args"):
            desc_parts.append(str(representative.attributes["args"])[:200])
        if representative.attributes.get("dns_query"):
            desc_parts.append(f"dns: {representative.attributes['dns_query']}")
        description = " ".join(desc_parts)

        runbooks = _fetch_runbooks(description, k=2)

        # Assemble grounded prompt and call the LLM
        group_summary = {
            "host": host,
            "severity": severity,
            "event_count": len(group_events),
            "evil_count": evil_count,
            "sus_count": sus_count,
            "window_start": window_start,
            "window_end": window_end,
            "event_types": event_types,
            "top_processes": top_processes,
        }
        prompt = _build_prompt(group_summary, similar_events, runbooks)
        llm_summary = _call_gateway_chat(prompt, caller_id)

        group_id = hashlib.md5(group_key.encode()).hexdigest()[:12]

        incident_groups.append(IncidentGroup(
            group_id=group_id,
            host=host,
            window_start=window_start,
            window_end=window_end,
            event_count=len(group_events),
            evil_count=evil_count,
            sus_count=sus_count,
            severity=severity,
            event_types=event_types,
            top_processes=top_processes,
            events=[_event_to_summary(e) for e in group_events[:20]],  # cap at 20 for response size
            similar_events=[{k: v for k, v in ev.items() if k != "content"} for ev in similar_events],
            runbooks=[{k: v for k, v in rb.items() if k != "embedding"} for rb in runbooks],
            llm_summary=llm_summary,
        ))

    # Sort groups so highest severity appears first
    incident_groups.sort(key=lambda g: _SEVERITY_ORDER.get(g.severity, 0), reverse=True)

    overall_severity = incident_groups[0].severity if incident_groups else "low"
    report_id = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:16]

    return IncidentReport(
        report_id=report_id,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        total_events=len(events),
        total_groups=len(incident_groups),
        overall_severity=overall_severity,
        groups=incident_groups,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    retrieve_ok = False
    gateway_ok  = False
    es_ok       = False

    try:
        r = requests.get(f"{_RETRIEVE_URL}/health", timeout=2)
        retrieve_ok = r.status_code == 200
    except Exception:
        pass

    try:
        r = requests.get(f"{_GATEWAY_URL}/health", timeout=2)
        gateway_ok = r.status_code == 200
    except Exception:
        pass

    try:
        from elasticsearch import Elasticsearch
        es = Elasticsearch(_ES_URL)
        es_ok = es.ping()
    except Exception:
        pass

    return {
        "status": "ok",
        "dependencies": {
            "context_retrieval": "up" if retrieve_ok else "down",
            "genai_gateway":     "up" if gateway_ok  else "down",
            "elasticsearch":     "up" if es_ok        else "down",
        },
        "retrieve_url": _RETRIEVE_URL,
        "gateway_url":  _GATEWAY_URL,
        "default_window_minutes": _WINDOW_MINUTES,
    }


@app.post("/analyze", response_model=IncidentReport)
def analyze(req: AnalyzeRequest):
    """
    Analyze a list of pre-fetched events and return a structured incident report.

    Use this endpoint when the caller already has the events (e.g., the output
    of the /predict endpoint in Phase 2B). For querying ES directly, use
    /analyze-from-es.
    """
    try:
        return _analyze_groups(req.events, req.window_minutes, req.caller_id)
    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze-from-es", response_model=IncidentReport)
def analyze_from_es(req: AnalyzeFromEsRequest):
    """
    Query ES for recent sus/evil events and analyze them.

    NOTE: The BETH dataset uses fixed historical timestamps (2021), so
    'lookback_minutes' will return 0 events unless you run against live data.
    This endpoint is architected for production use with real-time event streams.
    """
    from elasticsearch import Elasticsearch

    es = Elasticsearch(_ES_URL)
    if not es.ping():
        raise HTTPException(status_code=503, detail="Elasticsearch unreachable.")

    should_clauses = []
    if req.min_sus:
        should_clauses.append({"term": {"labels.sus": 1}})
    if req.min_evil:
        should_clauses.append({"term": {"labels.evil": 1}})

    if not should_clauses:
        raise HTTPException(status_code=422, detail="At least one of min_sus or min_evil must be >= 1.")

    query = {
        "bool": {
            "must": [{"bool": {"should": should_clauses, "minimum_should_match": 1}}],
            "filter": [{"range": {"timestamp": {
                "gte": f"now-{req.lookback_minutes}m",
                "lte": "now",
            }}}],
        }
    }

    resp = es.search(
        index=_ES_INDEX,
        body={"query": query, "size": req.max_events, "sort": [{"timestamp": "asc"}]},
    )
    hits = resp["hits"]["hits"]

    if not hits:
        return IncidentReport(
            report_id=hashlib.md5(b"empty").hexdigest()[:16],
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
            total_events=0,
            total_groups=0,
            overall_severity="low",
            groups=[],
        )

    events = []
    for h in hits:
        src = h["_source"]
        events.append(EventInput(
            log_type=src.get("log_type", ""),
            attributes=src.get("attributes", {}),
            labels=src.get("labels", {}),
            features=src.get("features", {}),
            timestamp=src.get("timestamp", ""),
            source_file=src.get("source_file", ""),
        ))

    try:
        return _analyze_groups(events, req.window_minutes, req.caller_id)
    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
