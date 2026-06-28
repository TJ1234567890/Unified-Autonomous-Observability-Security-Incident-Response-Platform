"""
Phase 2B: XGBoost triage model training.

Pulls all documents from Elasticsearch using the scroll API, extracts
pre-computed features, trains two binary classifiers (sus and evil),
and saves the models to disk.

HOW TO RUN:
    python services/triage-model/train.py

OUTPUTS:
    services/triage-model/sus_model.joblib
    services/triage-model/evil_model.joblib
    services/triage-model/feature_columns.joblib  (column order for predict.py)
"""

import logging
import os
import sys

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from imblearn.over_sampling import BorderlineSMOTE
from imblearn.under_sampling import RandomUnderSampler
from sklearn.metrics import classification_report, precision_recall_curve, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
ES_INDEX = "beth-security-logs"
MODEL_DIR = os.path.dirname(__file__)

# Features pulled from each ES document's 'features' key.
# These were computed by extract_features() in the Beam pipeline.
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
# Categorical fields that get label-encoded to integers.
CATEGORICAL_FIELDS = ["log_type"]

# All feature columns in the final training matrix (order matters for predict.py).
ALL_FEATURE_COLS = DNS_FEATURES + KERNEL_HOST_FEATURES + CATEGORICAL_FIELDS


def scroll_all_documents(es: Elasticsearch) -> list[dict]:
    """
    Retrieve all documents from Elasticsearch using the scroll API.

    WHY SCROLL AND NOT PAGINATE:
    Regular pagination (from + size) requires a separate HTTP request per page.
    At page_size=500, 3.8M documents = 7,600 requests. The scroll API opens a
    search context once, then streams batches of documents until exhausted.
    Much fewer round trips, much faster for bulk data retrieval.
    """
    logger.info("Scrolling all documents from ES index '%s'...", ES_INDEX)

    docs = []
    page = es.search(
        index=ES_INDEX,
        scroll="5m",  # keep scroll context alive for 5 minutes between batches
        size=5000,
        body={"query": {"match_all": {}}, "_source": True},
    )
    scroll_id = page["_scroll_id"]
    batch = page["hits"]["hits"]

    while batch:
        docs.extend(batch)
        if len(docs) % 100_000 == 0:
            logger.info("  Scrolled %d documents so far...", len(docs))
        page = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = page["_scroll_id"]
        batch = page["hits"]["hits"]

    es.clear_scroll(scroll_id=scroll_id)
    logger.info("Scroll complete. Total documents: %d", len(docs))
    return docs


def build_dataframe(docs: list[dict]) -> pd.DataFrame:
    """
    Convert raw ES documents into a flat DataFrame of features + labels.

    Documents that are missing both features and labels are dropped.
    Missing feature values are filled with 0 (safe default -- absent feature
    means the event type didn't have that field, e.g. DNS docs have no
    feat_is_root_user).
    """
    rows = []
    for doc in docs:
        source = doc["_source"]
        features = source.get("features", {})
        labels = source.get("labels", {})

        sus = labels.get("sus")
        evil = labels.get("evil")
        if sus is None or evil is None:
            continue  # drop documents without labels

        row = {col: features.get(col, 0) for col in DNS_FEATURES + KERNEL_HOST_FEATURES}
        row["log_type"] = source.get("log_type", "unknown")
        row["timestamp"] = source.get("timestamp", "")
        row["sus"] = int(sus)
        row["evil"] = int(evil)
        rows.append(row)

    df = pd.DataFrame(rows)
    logger.info("Built DataFrame: %d rows, %d columns", len(df), len(df.columns))
    return df


def encode_categoricals(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Label-encode categorical string columns to integers.
    Returns the modified DataFrame and a dict of fitted encoders
    so predict.py can apply the same encoding to new events.
    """
    encoders = {}
    for col in CATEGORICAL_FIELDS:
        le = LabelEncoder()
        df[col] = le.fit_transform(df[col].astype(str))
        encoders[col] = le
    return df, encoders


def stratified_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Stratified 70/15/15 split preserving class proportions for sus and evil.

    WHY NOT PURELY CHRONOLOGICAL:
    The BETH dataset has attack events concentrated in specific time windows,
    not spread uniformly. A strict chronological split puts the entire attack
    campaign in train/val and leaves the test set with zero evil examples --
    making evaluation impossible. Stratified split guarantees each partition
    has proportional sus=1 and evil=1 representation so metrics are valid.

    Tradeoff: we lose strict temporal ordering. Acceptable here because
    security models are retrained regularly on new labeled data; the evaluation
    goal is measuring generalization quality, not strict temporal forecasting.
    """
    # Combined stratification key so both sus and evil proportions are preserved.
    strat_key = df["sus"].astype(str) + "_" + df["evil"].astype(str)

    train_val, test = train_test_split(
        df, test_size=0.15, stratify=strat_key, random_state=42
    )
    strat_key_tv = train_val["sus"].astype(str) + "_" + train_val["evil"].astype(str)
    train, val = train_test_split(
        train_val, test_size=0.15 / 0.85, stratify=strat_key_tv, random_state=42
    )

    logger.info("Split sizes -- train: %d, val: %d, test: %d", len(train), len(val), len(test))
    return train, val, test


BINARY_FEATURE_COLS = [
    "feat_dns_failed",
    "feat_is_root_user",
    "feat_return_failed",
    "feat_args_has_shell",
    "feat_args_has_network",
    "feat_args_has_sensitive_path",
]


def apply_borderline_smote(
    X: pd.DataFrame, y: pd.Series, label_name: str
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Oversample the minority class using Borderline SMOTE (kind='borderline-2').

    WHY BORDERLINE-2 NOT STANDARD SMOTE:
    Standard SMOTE picks any two minority examples at random and interpolates.
    This creates synthetic evil events spread across all of evil feature space,
    including the obvious easy cases (root + shell + sensitive path all at once).

    Borderline-2 only generates synthetics from minority examples that sit NEAR
    benign examples -- the ambiguous borderline cases. This produces hard-to-detect
    synthetic attacks: realistic feature combinations that look plausibly benign
    but are labeled evil.

    WHY ROUND BINARY FEATURES AFTER SMOTE:
    SMOTE interpolates numerically. feat_is_root_user=0.4 is not valid (it is
    either 0 or 1). Rounding restores valid binary values after synthesis.

    APPLIED TO TRAINING DATA ONLY -- never val or test. Applying before the
    split would let synthetic samples derived from training examples leak into
    the test set, making evaluation metrics invalid.
    """
    pos_count = (y == 1).sum()
    neg_count = (y == 0).sum()

    if pos_count == 0:
        logger.warning("%s: no positive examples -- skipping SMOTE.", label_name)
        return X, y

    if pos_count >= neg_count:
        logger.info("%s: already balanced -- skipping SMOTE.", label_name)
        return X, y

    logger.info(
        "%s: applying BorderlineSMOTE (before: %d pos, %d neg)...",
        label_name, pos_count, neg_count,
    )

    # Step 1: undersample majority to 5:1 ratio before SMOTE.
    # BorderlineSMOTE's k-NN search is O(n) per minority point.
    # Reducing 2.6M negatives to ~5x positives shrinks the search space
    # dramatically while preserving the borderline region structure.
    target_neg = min(neg_count, pos_count * 5)
    under = RandomUnderSampler(
        sampling_strategy={0: target_neg, 1: pos_count}, random_state=42
    )
    X_under, y_under = under.fit_resample(X, y)
    logger.info("%s: after undersampling: %d pos, %d neg", label_name, pos_count, target_neg)

    # Step 2: BorderlineSMOTE on the reduced dataset to reach 1:1 balance.
    smote = BorderlineSMOTE(kind="borderline-2", random_state=42)
    X_res, y_res = smote.fit_resample(X_under, y_under)

    X_res = pd.DataFrame(X_res, columns=X.columns)

    binary_cols = [c for c in BINARY_FEATURE_COLS if c in X_res.columns]
    X_res[binary_cols] = X_res[binary_cols].round().astype(int).clip(0, 1)

    new_pos = (y_res == 1).sum()
    new_neg = (y_res == 0).sum()
    logger.info("%s: after SMOTE: %d pos, %d neg", label_name, new_pos, new_neg)
    return X_res, y_res


def compute_scale_pos_weight(labels: pd.Series) -> float:
    """
    Compute class weight ratio for imbalanced binary labels.
    XGBoost multiplies the gradient for positive (minority) examples
    by this factor, forcing the model to pay more attention to rare events.
    """
    neg = (labels == 0).sum()
    pos = (labels == 1).sum()
    if pos == 0:
        raise ValueError("No positive examples in training set -- cannot train.")
    weight = neg / pos
    logger.info("  Class balance: %d negative, %d positive -> scale_pos_weight=%.2f", neg, pos, weight)
    return float(weight)


def train_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    label_name: str,
) -> xgb.Booster:
    """
    Train a binary XGBoost classifier on GPU.

    num_boost_round: number of trees in the ensemble. Each tree corrects
    the residual errors left by all previous trees.

    early_stopping_rounds: if validation loss does not improve for 30
    consecutive trees, stop adding trees. Prevents overfitting and saves time.
    """
    logger.info("Training %s model on GPU...", label_name)

    spw = compute_scale_pos_weight(y_train)

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    # Base params shared by both models.
    # Sus params were Optuna-tuned (50 trials). Evil uses defaults -- already strong.
    SUS_PARAMS = {
        "max_depth": 5,
        "learning_rate": 0.225087,
        "subsample": 0.586104,
        "colsample_bytree": 0.903578,
        "min_child_weight": 6,
        "gamma": 0.399563,
        "reg_alpha": 1.358576,
        "reg_lambda": 4.009962,
    }
    EVIL_PARAMS = {
        "max_depth": 6,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 1,
        "gamma": 0.0,
        "reg_alpha": 0.0,
        "reg_lambda": 1.0,
    }
    tuned = SUS_PARAMS if label_name == "sus" else EVIL_PARAMS

    params = {
        "device": "cuda",
        "tree_method": "hist",
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "scale_pos_weight": spw,
        "seed": 42,
        **tuned,
    }

    model = xgb.train(
        params,
        dtrain,
        num_boost_round=500,
        evals=[(dtrain, "train"), (dval, "val")],
        early_stopping_rounds=30,
        verbose_eval=50,
    )

    logger.info("%s model trained. Best iteration: %d", label_name, model.best_iteration)
    return model


def find_optimal_threshold(
    model: xgb.Booster,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    label_name: str,
    strategy: str = "f1",
) -> float:
    """
    Find the decision threshold that optimises a chosen metric on the
    validation set. Never touches the test set -- that stays sealed.

    strategy="f1"        -- maximise F1 (balances precision and recall)
    strategy="precision" -- find the lowest threshold where precision >= 0.70
                            while keeping recall above 0.50.

    WHY USE THE VALIDATION SET NOT THE TEST SET:
    The threshold is a decision -- you're choosing a value based on performance
    data. Any data used to make a decision becomes 'training' data in a broad
    sense. If you picked the threshold by looking at test set metrics, those
    test metrics would no longer be an honest measure of unseen performance.
    Validation set is the right place for all decisions. Test set is touched
    exactly once at the very end for the final honest score.
    """
    dval = xgb.DMatrix(X_val)
    probs = model.predict(dval)

    precisions, recalls, thresholds = precision_recall_curve(y_val, probs)

    if strategy == "f1":
        # F1 = 2 * P * R / (P + R). Maximise this across all thresholds.
        # precisions and recalls arrays have one more element than thresholds,
        # so we align them by dropping the last element of each.
        f1_scores = 2 * precisions[:-1] * recalls[:-1] / (
            precisions[:-1] + recalls[:-1] + 1e-9
        )
        best_idx = int(np.argmax(f1_scores))
        best_threshold = float(thresholds[best_idx])
        logger.info(
            "%s threshold (max-F1): %.3f  →  precision=%.3f  recall=%.3f  f1=%.3f",
            label_name, best_threshold,
            precisions[best_idx], recalls[best_idx], f1_scores[best_idx],
        )

    else:  # strategy == "precision"
        # Find the lowest threshold where precision >= 0.70 AND recall >= 0.50.
        # This trades some recall for much fewer false alarms.
        target_precision = 0.90
        min_recall = 0.50
        mask = (precisions[:-1] >= target_precision) & (recalls[:-1] >= min_recall)
        if mask.any():
            best_idx = int(np.argmax(mask))  # lowest threshold that meets both criteria
            best_threshold = float(thresholds[best_idx])
            logger.info(
                "%s threshold (precision>=%.2f): %.3f  →  precision=%.3f  recall=%.3f",
                label_name, target_precision, best_threshold,
                precisions[best_idx], recalls[best_idx],
            )
        else:
            best_threshold = 0.5
            logger.warning(
                "%s: no threshold achieves precision>=%.2f with recall>=%.2f. "
                "Falling back to 0.5.",
                label_name, target_precision, min_recall,
            )

    return best_threshold


def evaluate(
    model: xgb.Booster,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    label_name: str,
    threshold: float = 0.5,
):
    """Print precision, recall, F1, and ROC-AUC on the held-out test set."""
    dtest = xgb.DMatrix(X_test)
    probs = model.predict(dtest)
    preds = (probs >= threshold).astype(int)

    logger.info("\n=== %s TEST RESULTS (threshold=%.3f) ===", label_name.upper(), threshold)
    logger.info("\n%s", classification_report(y_test, preds, digits=4))
    if y_test.sum() > 0:
        auc = roc_auc_score(y_test, probs)
        logger.info("ROC-AUC: %.4f", auc)


def main():
    es = Elasticsearch(ES_URL)
    if not es.ping():
        logger.error("Cannot reach Elasticsearch at %s. Is Docker running?", ES_URL)
        sys.exit(1)

    docs = scroll_all_documents(es)

    df = build_dataframe(docs)
    if df.empty:
        logger.error("No usable documents found. Re-ingest with the fixed ingestor first.")
        sys.exit(1)

    df, encoders = encode_categoricals(df)

    train, val, test = stratified_split(df)

    feature_cols = [c for c in ALL_FEATURE_COLS if c in df.columns]

    X_train, X_val, X_test = train[feature_cols], val[feature_cols], test[feature_cols]

    # Apply Borderline SMOTE to training set only.
    # Val and test are never touched -- they must reflect the real distribution.
    X_train_sus, y_train_sus = apply_borderline_smote(X_train, train["sus"], "sus")
    X_train_evil, y_train_evil = apply_borderline_smote(X_train, train["evil"], "evil")

    sus_model = train_model(X_train_sus, y_train_sus, X_val, val["sus"], "sus")
    evil_model = train_model(X_train_evil, y_train_evil, X_val, val["evil"], "evil")

    # Find optimal thresholds on validation set (never on test set).
    # sus: maximise F1 -- early warning system, balance precision and recall.
    # evil: target precision >= 0.70 -- reduce false alarms on confirmed attacks.
    sus_threshold = find_optimal_threshold(sus_model, X_val, val["sus"], "sus", strategy="f1")
    evil_threshold = find_optimal_threshold(evil_model, X_val, val["evil"], "evil", strategy="precision")

    evaluate(sus_model, X_test, test["sus"], "sus", threshold=sus_threshold)
    evaluate(evil_model, X_test, test["evil"], "evil", threshold=evil_threshold)

    joblib.dump(sus_model, os.path.join(MODEL_DIR, "sus_model.joblib"))
    joblib.dump(evil_model, os.path.join(MODEL_DIR, "evil_model.joblib"))
    joblib.dump(
        {
            "encoders": encoders,
            "feature_cols": feature_cols,
            "sus_threshold": sus_threshold,
            "evil_threshold": evil_threshold,
        },
        os.path.join(MODEL_DIR, "feature_columns.joblib"),
    )

    logger.info("Models saved to %s", MODEL_DIR)
    logger.info("Thresholds: sus=%.3f  evil=%.3f", sus_threshold, evil_threshold)


if __name__ == "__main__":
    main()
