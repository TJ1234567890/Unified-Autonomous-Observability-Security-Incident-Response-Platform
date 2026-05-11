"""
Beam Consumer Pipeline — reads messages from Kafka, parses them, writes to Elasticsearch.

ARCHITECTURE:
    Kafka (security.logs.raw)
        → confluent-kafka consumer pulls messages into a Python list
        → Beam pipeline processes them:
            1. Parse JSON string → Python dict
            2. Route to correct parser (DnsParser, DeepKernelParser, StandardHostParser)
            3. Write cleaned documents to Elasticsearch

WHY TWO LAYERS:
    Beam's built-in ReadFromKafka requires a Java expansion service (it's a
    cross-language transform). That's the right choice for production on Flink/Dataflow,
    but overkill for local dev with DirectRunner. So we separate:
        - Layer 1: Kafka consumption (confluent-kafka, same library as the producer)
        - Layer 2: Beam pipeline (parsing + ES writes)
    When deploying to Flink later, Layer 1 gets replaced by ReadFromKafka — one swap.
"""

import json
import math
import logging
import os

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
# Layer 1: Kafka Consumer — pulls a batch of messages from the topic
# ---------------------------------------------------------------------------

CONSUMER_MODE = os.getenv("CONSUMER_MODE", "dev")


def consume_from_kafka(max_messages=1000, timeout_sec=10):
    """
    Pull up to max_messages from Kafka, then stop.

    TWO MODES controlled by CONSUMER_MODE in .env:

    DEV mode (default):
        Uses assign() with OFFSET_BEGINNING. Reads all messages from the start
        every run. Deterministic and instant — no rebalance, no group coordination.
        Use this locally when you always want to reprocess the full dataset.

    PRODUCTION mode:
        Uses subscribe() so Kafka distributes partitions across multiple workers.
        Resumes from the last committed offset — workers never re-read messages
        already processed. Run the same script on 5 machines and they each get
        a share of the partitions without stepping on each other.

    WHY THE DISTINCTION:
        In production you NEVER want to reset to beginning — that would reprocess
        every event ever received. Workers resume from where they stopped. That's
        the entire value of consumer groups. Reading from beginning is only for
        dev/testing where you want a clean fresh run every time.
    """
    consumer_config = {
        "bootstrap.servers": KAFKA_PRODUCER_CONFIG["bootstrap.servers"],
        "security.protocol": KAFKA_PRODUCER_CONFIG["security.protocol"],
        "sasl.mechanisms": KAFKA_PRODUCER_CONFIG["sasl.mechanisms"],
        "sasl.username": KAFKA_PRODUCER_CONFIG["sasl.username"],
        "sasl.password": KAFKA_PRODUCER_CONFIG["sasl.password"],
        "group.id": "beam-ingestor",
        "auto.offset.reset": "earliest",
    }

    consumer = Consumer(consumer_config)

    if CONSUMER_MODE == "production":
        # subscribe() — Kafka assigns partitions via the group coordinator.
        # Multiple workers each get a subset of partitions. Kafka tracks offsets
        # per group so each message is processed by exactly one worker.
        # We wait for the rebalance to complete before polling for messages.
        logger.info("PRODUCTION mode: subscribing to topic (group-managed partitions)...")
        consumer.subscribe([KAFKA_TOPIC])
        logger.info("Waiting for partition assignment via rebalance...")
        while not consumer.assignment():
            consumer.poll(1.0)
        logger.info(f"Partitions assigned: {[p.partition for p in consumer.assignment()]}")
    else:
        # assign() — we directly take all partitions at offset 0.
        # Instant, deterministic, always reads from the beginning.
        # No group coordinator, no rebalance. Right for local dev.
        logger.info("DEV mode: assigning all partitions from beginning...")
        metadata = consumer.list_topics(KAFKA_TOPIC, timeout=10)
        partition_ids = list(metadata.topics[KAFKA_TOPIC].partitions.keys())
        partitions = [TopicPartition(KAFKA_TOPIC, pid, OFFSET_BEGINNING) for pid in partition_ids]
        consumer.assign(partitions)
        logger.info(f"Assigned partitions {partition_ids} at offset 0.")

    messages = []
    empty_polls = 0

    logger.info(f"Consuming from '{KAFKA_TOPIC}' (max {max_messages} messages)...")

    while len(messages) < max_messages:
        # poll(1.0) = wait up to 1 second for a message.
        # Returns a Message object (with .value(), .key(), .error()) or None.
        msg = consumer.poll(1.0)

        if msg is None:
            empty_polls += 1
            # If we've waited timeout_sec seconds with no messages, assume
            # we've drained the topic and stop.
            if empty_polls >= timeout_sec:
                logger.info("No more messages available. Stopping consumption.")
                break
            continue

        if msg.error():
            # KafkaError._PARTITION_EOF means we've read to the end of a
            # partition. It's informational, not a real error.
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            logger.error(f"Kafka error: {msg.error()}")
            continue

        # Reset the empty poll counter since we got a real message.
        empty_polls = 0
        messages.append(msg.value().decode("utf-8"))

    # commit() tells Kafka: "I've successfully processed these messages,
    # update my group's offset so I don't re-read them next time."
    consumer.commit()
    consumer.close()

    logger.info(f"Consumed {len(messages)} messages from Kafka.")
    return messages


# ---------------------------------------------------------------------------
# Layer 2: Beam Pipeline — parse and write to Elasticsearch
# ---------------------------------------------------------------------------

def clean_nan(value):
    """
    Replace NaN/Infinity with None so the JSON is valid for Elasticsearch.

    WHY THIS EXISTS:
    pandas represents missing values as float('nan'). When json.dumps()
    serializes NaN, it writes the literal text 'NaN' — which is NOT valid JSON.
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

    For our simple parsers this doesn't matter, but it's the correct habit
    for when you later add DoFns that open ES connections or load ML models.
    """

    def setup(self):
        self.parsers = {
            "dns": DnsParser(),
            "deep_kernel": DeepKernelParser(),
            "standard_host": StandardHostParser(),
        }

    def process(self, element):
        """
        Takes a raw Kafka message (Python dict with 'log_type' and 'raw'),
        routes it to the correct parser, yields the cleaned result.
        """
        log_type = element.get("log_type")
        raw_data = element.get("raw", {})

        parser = self.parsers.get(log_type)
        if parser is None:
            logger.warning(f"Unknown log_type: '{log_type}'. Skipping message.")
            return  # yield nothing — message is dropped, pipeline continues

        parsed = parser.parse(raw_data)
        # Attach the source metadata so we can trace where this doc came from.
        parsed["source_file"] = element.get("source_file", "unknown")
        yield parsed


class WriteToEs(beam.DoFn):
    """
    Beam DoFn that bulk-writes documents to Elasticsearch.

    WHY A DoFn AND NOT beam.Map():
    We need setup() to create the ES client once per worker, and we need
    to batch documents for efficient bulk writes. Beam's bundling mechanism
    groups elements before calling process(), but we want explicit control
    over the bulk size for ES.

    WHY setup()/teardown() FOR THE ES CLIENT:
    The Elasticsearch client holds a persistent HTTP connection pool. Creating
    it once in setup() and reusing it across all process() calls is efficient.
    teardown() doesn't strictly need to close it (Python GC handles it), but
    it's good practice — especially when this DoFn eventually runs on Flink
    with long-lived workers.
    """

    def setup(self):
        self.es_client = Elasticsearch(ELASTIC_URL)
        self.buffer = []
        self.bulk_size = 500

    def process(self, element):
        action = {
            "_index": ES_INDEX_NAME,
            "_source": element,
        }
        self.buffer.append(action)

        if len(self.buffer) >= self.bulk_size:
            self._flush()

    def finish_bundle(self):
        """
        Called by Beam after it finishes sending a batch of elements to process().
        This ensures any leftover documents in the buffer get written to ES.
        Without this, the last partial batch (e.g., 200 docs when bulk_size=500)
        would be lost.
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
    to Flink, you change PipelineOptions and the Kafka consumption layer —
    the DoFns (ParseLogFn, WriteToEs) stay identical.
    """
    options = PipelineOptions(runner="DirectRunner")

    with beam.Pipeline(options=options) as p:
        (
            p
            # beam.Create() takes a Python list and turns it into a PCollection.
            # Each string in the list becomes one element in the pipeline.
            | "Inject messages" >> beam.Create(messages)

            # Parse each JSON string into a Python dict.
            # clean_nan is applied first to handle NaN values from pandas.
            | "Parse JSON" >> beam.Map(lambda raw: clean_nan(json.loads(raw)))

            # Route each dict to the correct parser based on log_type.
            | "Transform logs" >> beam.ParDo(ParseLogFn())

            # Write parsed documents to Elasticsearch.
            | "Write to ES" >> beam.ParDo(WriteToEs())
        )

    logger.info("Beam pipeline finished.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Layer 1: Pull messages from Kafka
    messages = consume_from_kafka(max_messages=10000, timeout_sec=10)

    if not messages:
        logger.info("No messages to process. Exiting.")
        return

    # Layer 2: Process through Beam and write to ES
    run_pipeline(messages)


if __name__ == "__main__":
    main()
