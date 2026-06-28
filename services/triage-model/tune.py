"""
Phase 2B: Hyperparameter tuning for sus and evil XGBoost classifiers.

Uses Optuna's Bayesian optimization (TPE sampler) to search the parameter
space efficiently. Each trial trains a model with a different combination
of hyperparameters and reports validation AUCPR back to Optuna. Optuna
focuses subsequent trials on regions that performed well in prior trials.

HOW TO RUN:
    python services/triage-model/tune.py --label sus --trials 50
    python services/triage-model/tune.py --label evil --trials 50

    Omit --trials to use the default (30). More trials = better results
    but longer runtime. 30-50 is sufficient for this parameter space.

OUTPUTS:
    Prints best hyperparameters at the end.
    Copy them into train.py's params dict to retrain the final model.
"""

import argparse
import logging
import os
import sys

import joblib
import numpy as np
import optuna
import pandas as pd
import xgboost as xgb
from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from imblearn.over_sampling import BorderlineSMOTE
from imblearn.under_sampling import RandomUnderSampler
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
optuna.logging.set_verbosity(optuna.logging.WARNING)
logger = logging.getLogger(__name__)

ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
ES_INDEX = "beth-security-logs"

DNS_FEATURES = [
    "feat_dns_query_length",
    "feat_dns_query_entropy",
    "feat_dns_num_subdomains",
    "feat_dns_failed",
]
KERNEL_HOST_FEATURES = [
    "feat_is_root_user",
    "feat_return_failed",
    "feat_args_length",
    "feat_args_has_shell",
    "feat_args_has_network",
    "feat_args_has_sensitive_path",
]
CATEGORICAL_FIELDS = ["log_type"]
ALL_FEATURE_COLS = DNS_FEATURES + KERNEL_HOST_FEATURES + CATEGORICAL_FIELDS

BINARY_FEATURE_COLS = [
    "feat_dns_failed",
    "feat_is_root_user",
    "feat_return_failed",
    "feat_args_has_shell",
    "feat_args_has_network",
    "feat_args_has_sensitive_path",
]


# ---------------------------------------------------------------------------
# Data loading (same as train.py -- pulled once, cached in memory for speed)
# ---------------------------------------------------------------------------

def scroll_all_documents(es: Elasticsearch) -> list[dict]:
    docs = []
    page = es.search(
        index=ES_INDEX, scroll="5m", size=5000,
        body={"query": {"match_all": {}}, "_source": True},
    )
    scroll_id = page["_scroll_id"]
    batch = page["hits"]["hits"]
    while batch:
        docs.extend(batch)
        page = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = page["_scroll_id"]
        batch = page["hits"]["hits"]
    es.clear_scroll(scroll_id=scroll_id)
    return docs


def build_dataframe(docs: list[dict]) -> pd.DataFrame:
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


def prepare_splits(df: pd.DataFrame, label: str):
    """Encode, split, and apply SMOTE. Returns X_train, y_train, X_val, y_val, X_test, y_test."""
    le = LabelEncoder()
    df = df.copy()
    df["log_type"] = le.fit_transform(df["log_type"].astype(str))

    strat_key = df["sus"].astype(str) + "_" + df["evil"].astype(str)
    train_val, test = train_test_split(df, test_size=0.15, stratify=strat_key, random_state=42)
    strat_tv = train_val["sus"].astype(str) + "_" + train_val["evil"].astype(str)
    train, val = train_test_split(train_val, test_size=0.15 / 0.85, stratify=strat_tv, random_state=42)

    feature_cols = [c for c in ALL_FEATURE_COLS if c in df.columns]
    X_train = train[feature_cols]
    y_train = train[label]
    X_val = val[feature_cols]
    y_val = val[label]
    X_test = test[feature_cols]
    y_test = test[label]

    # Apply SMOTE to training set only.
    pos_count = (y_train == 1).sum()
    neg_count = (y_train == 0).sum()
    if pos_count > 0 and pos_count < neg_count:
        target_neg = min(neg_count, pos_count * 5)
        under = RandomUnderSampler(
            sampling_strategy={0: target_neg, 1: pos_count}, random_state=42
        )
        X_under, y_under = under.fit_resample(X_train, y_train)
        smote = BorderlineSMOTE(kind="borderline-2", random_state=42)
        X_train, y_train = smote.fit_resample(X_under, y_under)
        X_train = pd.DataFrame(X_train, columns=feature_cols)
        binary_cols = [c for c in BINARY_FEATURE_COLS if c in X_train.columns]
        X_train[binary_cols] = X_train[binary_cols].round().astype(int).clip(0, 1)
        y_train = pd.Series(y_train)

    return X_train, y_train, X_val, y_val, X_test, y_test, feature_cols


# ---------------------------------------------------------------------------
# Optuna objective
# ---------------------------------------------------------------------------

def make_objective(X_train, y_train, X_val, y_val):
    """
    Returns an objective function for Optuna to minimize/maximize.

    Each call receives a Trial object from Optuna. The trial suggests one
    value per hyperparameter from the ranges defined below. We train a model
    with those values and return the validation AUCPR. Optuna maximizes this.

    TPE (Tree-structured Parzen Estimator): Optuna's default sampler. It
    models the distribution of good hyperparameters vs bad hyperparameters
    separately, then samples from parameter regions that were more likely
    to produce good results in prior trials. Smarter than random sampling.
    """
    neg = (y_train == 0).sum()
    pos = (y_train == 1).sum()
    spw = float(neg / pos) if pos > 0 else 1.0

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    def objective(trial: optuna.Trial) -> float:
        params = {
            "device": "cuda",
            "tree_method": "hist",
            "objective": "binary:logistic",
            "eval_metric": "aucpr",
            "scale_pos_weight": spw,
            "seed": 42,
            # --- parameters Optuna will tune ---
            "max_depth": trial.suggest_int("max_depth", 3, 8),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.3, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
            "gamma": trial.suggest_float("gamma", 0.0, 5.0),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 10.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.0, 10.0),
        }

        model = xgb.train(
            params,
            dtrain,
            num_boost_round=400,
            evals=[(dval, "val")],
            early_stopping_rounds=30,
            verbose_eval=False,
        )

        # Return the best validation AUCPR this model achieved.
        return model.best_score

    return objective


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tune XGBoost hyperparameters with Optuna.")
    parser.add_argument("--label", choices=["sus", "evil"], required=True,
                        help="Which label to tune: sus or evil")
    parser.add_argument("--trials", type=int, default=30,
                        help="Number of Optuna trials (default: 30)")
    args = parser.parse_args()

    print(f"\nConnecting to Elasticsearch...")
    es = Elasticsearch(ES_URL)
    if not es.ping():
        print(f"ERROR: Cannot reach Elasticsearch at {ES_URL}. Is Docker running?")
        sys.exit(1)

    print("Scrolling documents from ES (this takes a few minutes)...")
    docs = scroll_all_documents(es)
    df = build_dataframe(docs)
    print(f"Loaded {len(df):,} documents.\n")

    print("Preparing splits and applying SMOTE (may take several minutes)...")
    X_train, y_train, X_val, y_val, X_test, y_test, feature_cols = prepare_splits(df, args.label)
    print(f"Train: {len(X_train):,} rows | Val: {len(X_val):,} rows | Test: {len(X_test):,} rows\n")

    print(f"Starting Optuna tuning: {args.trials} trials for '{args.label}' label...")
    print("Each trial trains one model. Watch for val-aucpr improving across trials.\n")

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    objective = make_objective(X_train, y_train, X_val, y_val)

    def trial_callback(study, trial):
        print(f"  Trial {trial.number:>3} | val-aucpr: {trial.value:.5f} | "
              f"best so far: {study.best_value:.5f}")

    study.optimize(objective, n_trials=args.trials, callbacks=[trial_callback])

    print(f"\n{'='*60}")
    print(f"BEST TRIAL: {study.best_trial.number}")
    print(f"BEST VAL-AUCPR: {study.best_value:.5f}")
    print(f"\nBEST PARAMETERS (copy into train.py):")
    for k, v in study.best_params.items():
        if isinstance(v, float):
            print(f"  \"{k}\": {v:.6f},")
        else:
            print(f"  \"{k}\": {v},")
    print(f"{'='*60}\n")

    # Save study so you can inspect all trials later.
    study_path = os.path.join(os.path.dirname(__file__), f"{args.label}_study.joblib")
    joblib.dump(study, study_path)
    print(f"Full study saved to {study_path}")


if __name__ == "__main__":
    main()
