"""
Probe the evil model's precision-recall curve without retraining.

Loads the saved evil_model.joblib, reconstructs the exact same val split
(same random_state=42 as train.py), and prints what precision and recall
would be at every threshold target from 0.70 to 0.95.

Use this to decide if a target (e.g. precision=0.90) is achievable before
committing to retraining or CTGAN.

HOW TO RUN:
    python services/triage-model/probe_threshold.py
"""

import os
import sys

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from sklearn.metrics import precision_recall_curve, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
ES_INDEX = "beth-security-logs"
MODEL_DIR = os.path.dirname(__file__)

DNS_FEATURES = [
    "feat_dns_query_length", "feat_dns_query_entropy",
    "feat_dns_num_subdomains", "feat_dns_failed",
]
KERNEL_HOST_FEATURES = [
    "feat_is_root_user", "feat_return_failed", "feat_args_length",
    "feat_args_has_shell", "feat_args_has_network", "feat_args_has_sensitive_path",
]
ALL_FEATURE_COLS = DNS_FEATURES + KERNEL_HOST_FEATURES + ["log_type"]


def scroll_all_documents(es):
    print("Scrolling documents from ES (this takes a few minutes)...")
    docs = []
    page = es.search(index=ES_INDEX, scroll="5m", size=5000,
                     body={"query": {"match_all": {}}, "_source": True})
    scroll_id = page["_scroll_id"]
    batch = page["hits"]["hits"]
    while batch:
        docs.extend(batch)
        if len(docs) % 500_000 == 0:
            print(f"  {len(docs):,} documents scrolled...")
        page = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = page["_scroll_id"]
        batch = page["hits"]["hits"]
    es.clear_scroll(scroll_id=scroll_id)
    print(f"  Done. {len(docs):,} total documents.\n")
    return docs


def build_dataframe(docs):
    rows = []
    for doc in docs:
        source = doc["_source"]
        features = source.get("features", {})
        labels = source.get("labels", {})
        sus = labels.get("sus")
        evil = labels.get("evil")
        if sus is None or evil is None:
            continue
        row = {col: features.get(col, 0) for col in DNS_FEATURES + KERNEL_HOST_FEATURES}
        row["log_type"] = source.get("log_type", "unknown")
        row["timestamp"] = source.get("timestamp", "")
        row["sus"] = int(sus)
        row["evil"] = int(evil)
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    # Load saved model and metadata.
    evil_model = joblib.load(os.path.join(MODEL_DIR, "evil_model.joblib"))
    meta = joblib.load(os.path.join(MODEL_DIR, "feature_columns.joblib"))
    encoders = meta["encoders"]
    feature_cols = meta["feature_cols"]

    print(f"Loaded evil_model.joblib")
    saved_threshold = meta.get("evil_threshold")
    threshold_str = f"{saved_threshold:.3f}" if saved_threshold is not None else "not saved"
    print(f"Current saved threshold: {threshold_str}\n")

    # Connect to ES.
    es = Elasticsearch(ES_URL)
    if not es.ping():
        print(f"ERROR: Cannot reach Elasticsearch at {ES_URL}. Is Docker running?")
        sys.exit(1)

    # Rebuild the exact same val split using the same random_state=42.
    docs = scroll_all_documents(es)
    df = build_dataframe(docs)
    print(f"Built DataFrame: {len(df):,} rows\n")

    # Apply same label encoding using saved encoders (not re-fitting).
    for col, le in encoders.items():
        df[col] = le.transform(df[col].astype(str))

    # Identical split logic to train.py -- same seed = same val rows.
    strat_key = df["sus"].astype(str) + "_" + df["evil"].astype(str)
    train_val, _ = train_test_split(df, test_size=0.15, stratify=strat_key, random_state=42)
    strat_tv = train_val["sus"].astype(str) + "_" + train_val["evil"].astype(str)
    _, val = train_test_split(train_val, test_size=0.15 / 0.85, stratify=strat_tv, random_state=42)

    X_val = val[feature_cols]
    y_val = val["evil"]

    print(f"Val set: {len(val):,} rows | evil=1: {y_val.sum():,} ({y_val.mean()*100:.2f}%)\n")

    # Run the model on val set.
    dval = xgb.DMatrix(X_val)
    probs = evil_model.predict(dval)

    roc = roc_auc_score(y_val, probs)
    print(f"ROC-AUC on val set: {roc:.4f}\n")

    # Build the full precision-recall curve.
    precisions, recalls, thresholds = precision_recall_curve(y_val, probs)

    # Print the tradeoff table at precision targets from 0.70 to 0.95.
    print("=" * 60)
    print("PRECISION-RECALL TRADEOFF (evil model, val set)")
    print("=" * 60)
    print(f"{'Target P':>10} {'Actual P':>10} {'Recall':>10} {'F1':>10} {'Threshold':>12}")
    print("-" * 60)

    for target_p in [0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
        mask = precisions[:-1] >= target_p
        if mask.any():
            idx = int(np.argmax(mask))  # lowest threshold that hits target precision
            p = precisions[idx]
            r = recalls[idx]
            f1 = 2 * p * r / (p + r + 1e-9)
            t = thresholds[idx]
            flag = " <-- BOTH 90/80" if p >= 0.90 and r >= 0.80 else ""
            print(f"{target_p:>10.0%} {p:>10.3f} {r:>10.3f} {f1:>10.3f} {t:>12.4f}{flag}")
        else:
            print(f"{target_p:>10.0%} {'N/A':>10} {'N/A':>10} {'N/A':>10} {'N/A':>12}")

    print("=" * 60)

    # Also show what recall is at the exact threshold where precision first hits 0.90.
    mask_90 = precisions[:-1] >= 0.90
    if mask_90.any():
        idx = int(np.argmax(mask_90))
        print(f"\nAt precision=0.90: recall={recalls[idx]:.3f}, threshold={thresholds[idx]:.4f}")
        if recalls[idx] >= 0.80:
            print(">>> P=0.90 AND R>=0.80 IS ACHIEVABLE. No CTGAN needed. Just update the threshold.")
        else:
            print(f">>> At P=0.90, recall drops to {recalls[idx]:.3f}.")
            print("    To reach R>=0.80 at P=0.90, the PR curve needs to shift up.")
            print("    CTGAN has a real job to do here.")
    else:
        print("\nPrecision never reaches 0.90 on this val set.")
        print("CTGAN is needed to shift the PR curve.")


if __name__ == "__main__":
    main()
