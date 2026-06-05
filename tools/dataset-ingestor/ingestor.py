"""
Beam Consumer Pipeline -- reads messages from Kafka, parses them, writes to Elasticsearch.

ARCHITECTURE:
    Kafka (security.logs.raw)
        -> confluent-kafka consumer pulls messages into a Python list
        -> Beam pipeline processes them:
            1. Parse JSON string -> Python dict
            2. Route to correct parser (DnsParser, DeepKernelParser, StandardHostParser)
            3. Write cleaned documents to Elasticsearch

WHY TWO LAYERS:
    Beam's built-in ReadFromKafka requires a Java expansion service (it's a
    cross-language transform). That's the right choice for production on Flink/Dataflow,
    but overkill for local dev with DirectRunner. So we separate:
        - Layer 1: Kafka consumption (confluent-kafka, same library as the producer)
        - Layer 2: Beam pipeline (parsing + ES writes)
    When deploying to Flink later, Layer 1 gets replaced by ReadFromKafka -- one swap.
"""

import hashlib
import json
import math
import re
import logging
import os
from collections import Counter

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from confluent_kafka import Consumer, KafkaError, TopicPartition, OFFSET_BEGINNING
from elasticsearch import Elasticsearch, helpers

from config import (
    KAFKA_PRODUCER_CONFIG,
    KAFKA_TOPIC,
    ELASTIC_URL,
    ES_INDEX_NAME,
)
from parsers import DnsParser, DeepKernelParser, StandardHostParser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature Extraction -- compiled at module load, reused for every document
# ---------------------------------------------------------------------------

_SHELL_RE = re.compile(r'/bin/bash|/bin/sh|/bin/zsh|cmd\.exe|powershell', re.IGNORECASE)
_NETWORK_RE = re.compile(r'\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bncat\b|\bsocat\b', re.IGNORECASE)
_SENSITIVE_PATH_RE = re.compile(r'/etc/passwd|/etc/shadow|/etc/sudoers|/root/|/proc/\d+', re.IGNORECASE)

_PROCESS_EXEC_EVENTS = {'execve', 'execveat', 'fork', 'vfork', 'clone'}
_FILE_ACCESS_EVENTS = {'openat', 'open', 'read', 'write', 'unlink', 'rename', 'mkdir', 'rmdir', 'stat', 'lstat', 'fstat'}
_NETWORK_EVENTS = {'connect', 'accept', 'bind', 'listen', 'socket', 'sendto', 'recvfrom', 'sendmsg', 'recvmsg'}
_MEMORY_EVENTS = {'mmap', 'mprotect', 'munmap', 'brk', 'mlock'}


def _shannon_entropy(s: str) -> float:
    """
    Compute Shannon entropy of a string. High entropy means high randomness.
    A domain like 'google.com' has low entropy (~2.5 bits). A DGA domain like
    'x7f3a9b2c.ru' has high entropy (~3.5+ bits) because characters are more
    uniformly distributed. Threshold ~3.5 is a reasonable malware signal.
    """
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def extract_features(element: dict) -> dict:
    """
    Compute ML-ready feature signals from a parsed document.

    WHY beam.Map AND NOT beam.ParDo:
    Every document in -> exactly one enriched document out. No zero-output case,
    no stateful setup needed (regex patterns are module-level constants, not
    per-worker connections). beam.Map is the right abstraction for pure 1-to-1
    transforms. beam.ParDo is for 0/1/many outputs or DoFn lifecycle (setup,
    finish_bundle, teardown).

    WHY features ARE NESTED UNDER 'features':
    Keeps the ES document schema clean. Parsed fields stay in their existing
    locations. Feature fields are grouped so any query or ML pipeline can select
    them with a single ES source filter: '_source.includes': ['features.*'].

    WHY DEFAULT IS 0 NOT None FOR BINARY FLAGS:
    For ML features we compute ourselves, 0 means 'not observed'. That IS the
    correct signal. None would mean 'unknown', which gets dropped or imputed
    during training -- wasting data. 0 is directly consumable by XGBoost.
    """
    features = {}
    attrs = element.get("attributes", {})

    if element.get("log_attribute") == "network_dns":
        query = str(attrs.get("dns_query") or "")
        response_code = attrs.get("dns_response_code")

        features["feat_dns_query_length"] = len(query)
        features["feat_dns_query_entropy"] = round(_shannon_entropy(query), 4)
        features["feat_dns_num_subdomains"] = query.count(".") if query else 0
        features["feat_dns_failed"] = 0 if response_code == 0 else 1

    else:
        args = str(attrs.get("args") or "")
        event_name = str(element.get("event_name") or "")

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

        if event_name in _PROCESS_EXEC_EVENTS:
            features["feat_event_category"] = "process_exec"
        elif event_name in _FILE_ACCESS_EVENTS:
            features["feat_event_category"] = "file_access"
        elif event_name in _NETWORK_EVENTS:
            features["feat_event_category"] = "network"
        elif event_name in _MEMORY_EVENTS:
            features["feat_event_category"] = "memory"
        else:
            features["feat_event_category"] = "other"

    return {**element, "features": features}


# ---------------------------------------------------------------------------
# Layer 1: Kafka Consumer -- builds consumer once, polls in batches
# ---------------------------------------------------------------------------

CONSUMER_MODE: str = os.getenv("CONSUMER_MODE", "dev")


def _build_consumer():
    """
    Create and configure the Kafka consumer. Called once in main() so the
    partition assignment and group rebalance happen only once, not once per batch.

    TWO MODES controlled by CONSUMER_MODE in .env:

    DEV mode (default):
        Uses assign() with OFFSET_BEGINNING. Always reads from offset 0 --
        bypasses committed offsets entirely. Right for local testing where
        you want a clean full replay every time.

    PRODUCTION mode:
        Uses subscribe() -- Kafka distributes partitions across workers via the
        group coordinator and resumes from the last committed offset. Multiple
        workers share the topic without stepping on each other.
    """
    consumer_config = {
        "bootstrap.servers": KAFKA_PRODUCER_CONFIG["bootstrap.servers"],
        "security.protocol": KAFKA_PRODUCER_CONFIG["security.protocol"],
        "sasl.mechanisms": KAFKA_PRODUCER_CONFIG["sasl.mechanisms"],
        "sasl.username": KAFKA_PRODUCER_CONFIG["sasl.username"],
        "sasl.password": KAFKA_PRODUCER_CONFIG["sasl.password"],
        "group.id": "beam-ingestor-v3",
        "auto.offset.reset": "earliest",
    }

    consumer = Consumer(consumer_config)

    if CONSUMER_MODE == "production":
        logger.info("PRODUCTION mode: subscribing to topic (group-managed partitions)...")
        consumer.subscribe([KAFKA_TOPIC])
        logger.info("Waiting for partition assignment via rebalance...")
        while not consumer.assignment():
            consumer.poll(1.0)
        logger.info(f"Partitions assigned: {[p.partition for p in consumer.assignment()]}")
    else:
        logger.info("DEV mode: assigning all partitions from beginning...")
        metadata = consumer.list_topics(KAFKA_TOPIC, timeout=10)
        partition_ids = list(metadata.topics[KAFKA_TOPIC].partitions.keys())
        partitions = [TopicPartition(KAFKA_TOPIC, pid, OFFSET_BEGINNING) for pid in partition_ids]
        consumer.assign(partitions)
        logger.info(f"Assigned partitions {partition_ids} at offset 0.")

    return consumer


def _poll_messages(consumer, max_messages=100000, timeout_sec=10):
    """
    Poll up to max_messages from an already-configured consumer.
    Does NOT commit -- main() commits only after the pipeline succeeds.

    WHY COMMIT IS NOT HERE:
    If commit() fires before run_pipeline(), a pipeline crash leaves Kafka's
    offset advanced past messages that never reached ES -- permanent data loss.
    Commit belongs in main(), after run_pipeline() returns without raising.
    """
    messages = []
    empty_polls = 0

    logger.info(f"Consuming from '{KAFKA_TOPIC}' (max {max_messages} messages)...")

    while len(messages) < max_messages:
        msg = consumer.poll(1.0)

        if msg is None:
            empty_polls += 1
            if empty_polls >= timeout_sec:
                logger.info("No more messages available. Stopping consumption.")
                break
            continue

        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            logger.error(f"Kafka error: {msg.error()}")
            continue

        empty_polls = 0
        messages.append(msg.value().decode("utf-8"))

    logger.info(f"Consumed {len(messages)} messages from Kafka.")
    return messages


# ---------------------------------------------------------------------------
# Layer 2: Beam Pipeline -- parse and write to Elasticsearch
# ---------------------------------------------------------------------------

def _make_doc_id(element: dict) -> str:
    """
    Deterministic document ID from stable fields in the parsed document.

    WHY THIS EXISTS:
    Without an explicit _id, Elasticsearch generates a random UUID per write.
    On re-runs (crash recovery, dev mode replays), the same source row produces
    a new UUID -> duplicates accumulate silently in the index.

    By hashing stable identifiers, the same row always produces the same _id.
    ES upserts (overwrites) instead of inserting -- idempotent regardless of
    how many times the pipeline replays the same data.

    WHY md5 AND NOT sha256:
    Collision resistance is irrelevant here -- this is not a security use case.
    md5 is faster and produces a 32-char hex string vs 64 for sha256.
    At 3.8M documents the storage difference is meaningful.
    """
    attrs = element.get("attributes", {})
    source_file = element.get("source_file", "")

    if element.get("log_attribute") == "network_dns":
        key = "|".join([
            source_file,
            str(element.get("timestamp", "")),
            str(attrs.get("source_ip", "")),
            str(attrs.get("dns_query", "")),
            str(attrs.get("dns_response_code", "")),
        ])
    else:
        key = "|".join([
            source_file,
            str(element.get("timestamp", "")),
            str(attrs.get("process_id", "")),
            str(element.get("event_name", "")),
            str(attrs.get("return_value", "")),
            str(attrs.get("args", "")),
        ])

    return hashlib.md5(key.encode()).hexdigest()


def clean_nan(value):
    """
    Replace NaN/Infinity with None so the JSON is valid for Elasticsearch.

    WHY THIS EXISTS:
    pandas represents missing values as float('nan'). When json.dumps()
    serializes NaN, it writes the literal text 'NaN' -- which is NOT valid JSON.
    Python's json.loads() happens to accept it (it's lenient), but Elasticsearch
    will reject documents containing NaN. So we scrub it here.
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: clean_nan(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_nan(v) for v in value]
    return value


class ParseLogFn(beam.DoFn):
    """
    Beam DoFn that routes each message to the correct parser.

    WHY setup() AND NOT __init__():
    Beam serializes DoFn objects and ships them to workers. The worker then
    calls setup() once to initialize. If you put heavy objects in __init__(),
    they'd need to be serialized too (and some things like DB connections
    can't be serialized). setup() runs AFTER the DoFn arrives at the worker,
    so it's safe to create anything there.
    """

    def setup(self):
        self.parsers = {
            "dns": DnsParser(),
            "deep_kernel": DeepKernelParser(),
            "standard_host": StandardHostParser(),
        }

    def process(self, element):
        log_type = element.get("log_type")
        raw_data = element.get("raw", {})

        parser = self.parsers.get(log_type)
        if parser is None:
            logger.warning(f"Unknown log_type: '{log_type}'. Skipping message.")
            return

        parsed = parser.parse(raw_data)
        parsed["source_file"] = element.get("source_file", "unknown")
        parsed["log_type"] = log_type
        yield parsed


class WriteToEs(beam.DoFn):
    """
    Beam DoFn that bulk-writes documents to Elasticsearch.

    WHY setup()/teardown() FOR THE ES CLIENT:
    The Elasticsearch client holds a persistent HTTP connection pool. Creating
    it once in setup() and reusing it across all process() calls is efficient.
    teardown() closes it after all elements are processed.
    """

    def setup(self):
        self.es_client = Elasticsearch(ELASTIC_URL)
        self.buffer = []
        self.bulk_size = 500

    def process(self, element):
        action = {
            "_index": ES_INDEX_NAME,
            "_id": _make_doc_id(element),
            "_source": element,
        }
        self.buffer.append(action)

        if len(self.buffer) >= self.bulk_size:
            self._flush()

    def finish_bundle(self):
        """
        Flush remaining documents after Beam stops calling process().
        Without this, the last partial batch would be silently lost.
        """
        self._flush()

    def _flush(self):
        if not self.buffer:
            return
        success, errors = helpers.bulk(self.es_client, self.buffer, raise_on_error=False)
        if errors:
            logger.error(f"ES bulk write errors: {errors}")
        else:
            logger.info(f"Wrote {success} documents to Elasticsearch.")
        self.buffer = []

    def teardown(self):
        self.es_client.close()


def run_pipeline(messages):
    """
    Build and execute the Beam pipeline.

    DirectRunner runs everything locally in one process. When you move
    to Flink, you change PipelineOptions and the Kafka consumption layer --
    the DoFns (ParseLogFn, WriteToEs) stay identical.
    """
    options = PipelineOptions(runner="DirectRunner")

    with beam.Pipeline(options=options) as p:
        (
            p
            | "Inject messages" >> beam.Create(messages)
            | "Parse JSON" >> beam.Map(lambda raw: clean_nan(json.loads(raw)))
            | "Transform logs" >> beam.ParDo(ParseLogFn())
            | "Extract features" >> beam.Map(extract_features)
            | "Write to ES" >> beam.ParDo(WriteToEs())
        )

    logger.info("Beam pipeline finished.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    consumer = _build_consumer()
    total_processed = 0
    run_count = 0

    try:
        while True:
            run_count += 1
            logger.info(f"--- Ingest run {run_count} ---")

            messages = _poll_messages(consumer, max_messages=100000, timeout_sec=10)

            if not messages:
                logger.info(
                    f"Topic exhausted after {run_count - 1} runs. "
                    f"Total documents written: {total_processed:,}."
                )
                break

            run_pipeline(messages)

            # Commit AFTER the pipeline succeeds. If run_pipeline() raises,
            # the exception propagates past this line -- Kafka re-delivers on
            # next startup, restoring at-least-once delivery semantics.
            consumer.commit()
            total_processed += len(messages)
            logger.info(f"Run {run_count} complete. {total_processed:,} total documents processed so far.")
    finally:
        # Runs whether we exited cleanly or via exception -- consumer is always
        # closed so Kafka can trigger a rebalance and reassign our partitions.
        consumer.close()


if __name__ == "__main__":
    main()
