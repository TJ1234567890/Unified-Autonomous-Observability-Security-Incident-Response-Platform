"""
Phase 2B: Real-time triage prediction API.

Loads the trained XGBoost models once at startup and exposes a /predict
endpoint that scores a single log event for suspicious and evil activity.

The feature extraction logic here mirrors extract_features() in
tools/dataset-ingestor/ingestor.py exactly -- same regex patterns, same
math, same defaults. If ingestor.py changes how features are computed,
this file must be updated to match or predictions will be wrong.

HOW TO RUN (from project root):
    .venv\\Scripts\\python -m uvicorn predict:app --app-dir services/triage-model --host 0.0.0.0 --port 8001

ENDPOINTS:
    GET  /health   -- liveness check, shows loaded thresholds
    POST /predict  -- score one log event, returns probabilities and flags
"""

import math
import os
import re
from collections import Counter
from typing import Any

import joblib
import pandas as pd
import xgboost as xgb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Load models at startup -- never inside the request handler.
# Creating an XGBoost DMatrix and loading a model takes milliseconds.
# Doing it per-request multiplies that cost by every single call.
# Module-level loading means it happens once when uvicorn starts the worker.
# ---------------------------------------------------------------------------

_MODEL_DIR = os.path.dirname(__file__)

try:
    _sus_model: xgb.Booster = joblib.load(os.path.join(_MODEL_DIR, "sus_model.joblib"))
    _evil_model: xgb.Booster = joblib.load(os.path.join(_MODEL_DIR, "evil_model.joblib"))
    _meta: dict = joblib.load(os.path.join(_MODEL_DIR, "feature_columns.joblib"))
except FileNotFoundError as e:
    raise RuntimeError(
        f"Model file not found: {e}. Run train.py first to generate the .joblib files."
    )

_ENCODERS: dict = _meta["encoders"]
_FEATURE_COLS: list[str] = _meta["feature_cols"]
_SUS_THRESHOLD: float = float(_meta.get("sus_threshold", 0.365))
_EVIL_THRESHOLD: float = float(_meta.get("evil_threshold", 0.8322))

# ---------------------------------------------------------------------------
# Feature extraction -- exact copy of the logic in ingestor.py.
# Same patterns, same defaults, same edge-case handling.
# ---------------------------------------------------------------------------

_SHELL_RE = re.compile(
    r'/bin/bash|/bin/sh|/bin/zsh|cmd\.exe|powershell', re.IGNORECASE
)
_NETWORK_RE = re.compile(
    r'\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bncat\b|\bsocat\b', re.IGNORECASE
)
_SENSITIVE_PATH_RE = re.compile(
    r'/etc/passwd|/etc/shadow|/etc/sudoers|/root/|/proc/\d+', re.IGNORECASE
)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _extract_features(log_type: str, attrs: dict) -> dict:
    """
    Convert a raw log event into the feature vector the model expects.

    All features default to 0. For a DNS event, kernel/host features are 0
    (and ignored by the model via the feature mask it learned). For kernel/host
    events, DNS features are 0. This matches the training pipeline exactly --
    build_dataframe() in train.py fills missing features with 0 for the same
    reason.
    """
    features: dict[str, Any] = {col: 0 for col in _FEATURE_COLS if col != "log_type"}

    if log_type == "dns":
        query = str(attrs.get("dns_query") or "")
        response_code = attrs.get("dns_response_code")
        features["feat_dns_query_length"] = len(query)
        features["feat_dns_query_entropy"] = round(_shannon_entropy(query), 4)
        features["feat_dns_num_subdomains"] = query.count(".") if query else 0
        features["feat_dns_failed"] = 0 if response_code == 0 else 1

    else:
        args = str(attrs.get("args") or "")

        try:
            rv = int(attrs.get("return_value") or 0)
        except (ValueError, TypeError):
            rv = 0

        try:
            uid = int(attrs.get("user_id") or -1)
        except (ValueError, TypeError):
            uid = -1

        features["feat_is_root_user"] = 1 if uid == 0 else 0
        features["feat_return_failed"] = 1 if rv < 0 else 0
        features["feat_args_length"] = len(args)
        features["feat_args_has_shell"] = 1 if _SHELL_RE.search(args) else 0
        features["feat_args_has_network"] = 1 if _NETWORK_RE.search(args) else 0
        features["feat_args_has_sensitive_path"] = 1 if _SENSITIVE_PATH_RE.search(args) else 0

    return features


def _encode_log_type(log_type: str) -> int:
    """
    Apply the same LabelEncoder that was fit during training.

    If an unknown log_type arrives, fall back to 0 rather than crashing.
    Unknown types will score poorly on both models (no features match), which
    is the safe behavior -- don't flag unknown events as confirmed attacks.
    """
    le = _ENCODERS.get("log_type")
    if le is None:
        return 0
    if log_type not in le.classes_:
        return 0
    return int(le.transform([log_type])[0])


def _build_dmatrix(log_type: str, attrs: dict) -> xgb.DMatrix:
    features = _extract_features(log_type, attrs)
    features["log_type"] = _encode_log_type(log_type)
    df = pd.DataFrame([features])
    for col in _FEATURE_COLS:
        if col not in df.columns:
            df[col] = 0
    return xgb.DMatrix(df[_FEATURE_COLS])


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Triage Model API", version="1.0.0")


class LogEvent(BaseModel):
    log_type: str = Field(..., examples=["dns", "deep_kernel", "standard_host"])
    attributes: dict[str, Any] = Field(default_factory=dict)


class TriageResult(BaseModel):
    sus_probability: float
    evil_probability: float
    sus_flag: bool
    evil_flag: bool
    sus_threshold: float
    evil_threshold: float


@app.get("/health")
def health():
    return {
        "status": "ok",
        "sus_threshold": _SUS_THRESHOLD,
        "evil_threshold": _EVIL_THRESHOLD,
        "feature_count": len(_FEATURE_COLS),
    }


@app.post("/predict", response_model=TriageResult)
def predict(event: LogEvent):
    try:
        dmat = _build_dmatrix(event.log_type, event.attributes)
        sus_prob = float(_sus_model.predict(dmat)[0])
        evil_prob = float(_evil_model.predict(dmat)[0])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TriageResult(
        sus_probability=round(sus_prob, 4),
        evil_probability=round(evil_prob, 4),
        sus_flag=sus_prob >= _SUS_THRESHOLD,
        evil_flag=evil_prob >= _EVIL_THRESHOLD,
        sus_threshold=_SUS_THRESHOLD,
        evil_threshold=_EVIL_THRESHOLD,
    )
