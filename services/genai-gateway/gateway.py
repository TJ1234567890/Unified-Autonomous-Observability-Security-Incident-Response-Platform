"""
Phase 4: Secure GenAI Gateway.

All LLM traffic from internal services flows through this gateway. It enforces:
  1. Per-caller rate limiting (token bucket, 60 req/min sustained, 10 burst)
  2. PII/secret detection -- prompts with credentials or PII are rejected
     before they reach the external LLM API
  3. Metadata-only audit logging -- caller, model, token counts, latency.
     Prompt text is deliberately NEVER stored (privacy by design).

WHY A GATEWAY AND NOT DIRECT LLM CALLS FROM EACH SERVICE:
    Without this gateway, every service that calls an LLM would need to
    independently implement rate limiting, secret scrubbing, and audit logging.
    That logic gets duplicated, diverges over time, and creates inconsistent
    security guarantees. One gateway enforces one policy for all LLM traffic.
    This is the reverse proxy pattern applied to AI model access.

ENVIRONMENT VARIABLES (.env):
    GEMINI_API_KEY      -- Google Gemini API key. If absent, /chat returns 503.
    GEMINI_MODEL        -- Model name (default: gemini-1.5-flash)
    GATEWAY_RPM         -- Rate limit in requests per minute per caller (default: 60)
    GATEWAY_BURST       -- Token bucket burst size (default: 10)

HOW TO RUN (from project root):
    .venv\\Scripts\\python -m uvicorn gateway:app --app-dir services/genai-gateway --host 0.0.0.0 --port 8080
"""

import logging
import os
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# config.py is in tools/dataset-ingestor/ -- add it to path for load_dotenv trigger
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))
load_dotenv()

from rate_limiter import RateLimiter
from pii_detector import scan as scan_pii, detected_types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read once at startup)
# ---------------------------------------------------------------------------

_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
_RATE_RPM       = float(os.getenv("GATEWAY_RPM", "60"))
_RATE_BURST     = float(os.getenv("GATEWAY_BURST", "10"))

# ---------------------------------------------------------------------------
# Module-level resources (created once; reused for every request)
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter(rate_per_minute=_RATE_RPM, burst=_RATE_BURST)

# In-memory audit log: ring buffer capped at 1000 entries.
# In production this would stream to a centralized log store (Loki, CloudWatch).
_audit_log: deque = deque(maxlen=1000)


@dataclass
class AuditEntry:
    ts_utc: str           # ISO timestamp (UTC)
    caller_id: str        # who called
    model: str            # which LLM model was targeted
    prompt_tokens: int    # approximate tokens in the prompt
    response_tokens: int  # tokens in the response
    latency_ms: int       # total wall-clock latency (includes LLM round-trip)
    blocked_pii: bool     # True if rejected before reaching the LLM
    pii_types: list       # which PII patterns triggered (empty if not blocked)
    success: bool         # True if the LLM responded successfully


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Secure GenAI Gateway", version="1.0.0")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str    = Field(..., pattern=r'^(user|assistant|system)$')
    content: str = Field(..., min_length=1, max_length=32_000)


class ChatRequest(BaseModel):
    messages:         list[ChatMessage] = Field(..., min_length=1, max_length=50)
    caller_id:        str   = Field("anonymous", min_length=1, max_length=64)
    temperature:      float = Field(0.2, ge=0.0, le=2.0)
    max_output_tokens: int  = Field(1024, ge=1, le=8192)


class ChatResponse(BaseModel):
    text:            str
    model:           str
    prompt_tokens:   int
    response_tokens: int
    latency_ms:      int


# ---------------------------------------------------------------------------
# Gemini call (lazy import so the gateway starts without the package)
# ---------------------------------------------------------------------------

def _call_gemini(messages: list[ChatMessage], temperature: float, max_tokens: int) -> dict:
    """
    Call the Gemini API. Returns {text, prompt_tokens, response_tokens}.

    google.generativeai is imported inside this function so the gateway can start
    and pass all tests even when the package is absent or GEMINI_API_KEY is unset.
    If the key is missing, callers get a 503 with a clear explanation.
    """
    try:
        import google.generativeai as genai  # type: ignore
    except ImportError:
        raise HTTPException(status_code=503, detail="google-generativeai not installed. Run: pip install google-generativeai")

    if not _GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not set in environment.")

    genai.configure(api_key=_GEMINI_API_KEY)

    model = genai.GenerativeModel(
        model_name=_GEMINI_MODEL,
        generation_config=genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        ),
    )

    # Gemini does not have a native "system" role. Prepend system instructions
    # to the first user message so they are always included in context.
    parts = []
    system_prefix = ""
    for msg in messages:
        if msg.role == "system":
            system_prefix = f"[System Instructions]: {msg.content}\n\n"
        else:
            role = "model" if msg.role == "assistant" else "user"
            content = system_prefix + msg.content if system_prefix else msg.content
            parts.append({"role": role, "parts": [content]})
            system_prefix = ""

    response = model.generate_content(parts)
    text = response.text

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count",
                             len(" ".join(m.content for m in messages).split()))
    response_tokens = getattr(usage, "candidates_token_count", len(text.split()))

    return {"text": text, "prompt_tokens": prompt_tokens, "response_tokens": response_tokens}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_configured": bool(_GEMINI_API_KEY),
        "model": _GEMINI_MODEL,
        "rate_limit_rpm": _RATE_RPM,
        "burst": _RATE_BURST,
        "audit_log_entries": len(_audit_log),
    }


@app.get("/audit-log")
def audit_log(limit: int = 100):
    """
    Return recent audit entries (metadata only -- no prompt content).

    This endpoint is for internal monitoring. In production it would be
    protected by internal-only routing rules (not exposed externally).
    """
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=422, detail="limit must be 1-1000")
    entries = list(_audit_log)[-limit:]
    return {"count": len(entries), "entries": [asdict(e) for e in entries]}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """
    Proxy a chat request to the Gemini LLM with rate limiting and PII scanning.

    Flow:
      1. Rate limit check per caller_id (token bucket)
      2. PII scan across all message content
      3. LLM call (Gemini API)
      4. Audit log entry (metadata only)
    """
    t0 = time.monotonic()

    # Step 1: rate limit
    if not _rate_limiter.allow(req.caller_id):
        logger.warning(f"Rate limit exceeded: caller='{req.caller_id}'")
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded for caller '{req.caller_id}'. Try again in a moment.",
        )

    # Step 2: PII scan
    full_text = " ".join(m.content for m in req.messages)
    pii_hits  = scan_pii(full_text)
    pii_types = list({h.pii_type for h in pii_hits})

    if pii_hits:
        _audit_log.append(AuditEntry(
            ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            caller_id=req.caller_id,
            model=_GEMINI_MODEL,
            prompt_tokens=0,
            response_tokens=0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            blocked_pii=True,
            pii_types=pii_types,
            success=False,
        ))
        logger.warning(f"PII blocked: caller='{req.caller_id}' types={pii_types}")
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "pii_detected",
                "types":   pii_types,
                "message": "Prompt contains sensitive data that cannot be forwarded to the LLM.",
            },
        )

    # Step 3: LLM call
    try:
        result = _call_gemini(req.messages, req.temperature, req.max_output_tokens)
        latency_ms = int((time.monotonic() - t0) * 1000)

        _audit_log.append(AuditEntry(
            ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            caller_id=req.caller_id,
            model=_GEMINI_MODEL,
            prompt_tokens=result["prompt_tokens"],
            response_tokens=result["response_tokens"],
            latency_ms=latency_ms,
            blocked_pii=False,
            pii_types=[],
            success=True,
        ))

        return ChatResponse(
            text=result["text"],
            model=_GEMINI_MODEL,
            prompt_tokens=result["prompt_tokens"],
            response_tokens=result["response_tokens"],
            latency_ms=latency_ms,
        )

    except HTTPException:
        raise
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        _audit_log.append(AuditEntry(
            ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            caller_id=req.caller_id,
            model=_GEMINI_MODEL,
            prompt_tokens=0,
            response_tokens=0,
            latency_ms=latency_ms,
            blocked_pii=False,
            pii_types=[],
            success=False,
        ))
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=503, detail=f"LLM call failed: {e}")
