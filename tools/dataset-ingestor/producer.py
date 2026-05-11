"""
Kafka Producer — reads CSV files and publishes RAW rows to Kafka.

KEY DESIGN DECISIONS:
1. We publish RAW rows (not parsed). The consumer (Beam) does the transformation.
   This lets us replay data and fix parsers without re-reading CSVs.
2. One message per row. Kafka handles batching at the network level internally.
3. The 'key' for each message is the source filename. This ensures all rows from
   the same file land on the same Kafka partition, preserving order within a file.
"""

import glob
import json
import os
import sys

import pandas as pd
from confluent_kafka import Producer

from config import KAFKA_PRODUCER_CONFIG, KAFKA_TOPIC


def delivery_report(err, msg):
    """
    Callback invoked once per message to confirm delivery.

    WHY THIS EXISTS:
    Kafka producing is asynchronous. When you call producer.produce(), the message
    goes into an internal buffer — it has NOT been sent yet. The actual network send
    happens in the background. This callback tells you whether each message actually
    made it to the broker, or if something went wrong (network timeout, auth failure,
    topic doesn't exist, etc.).

    In production, you'd log failures and potentially retry. For now, we just print
    errors so you can see them during development.
    """
    if err is not None:
        print(f"DELIVERY FAILED: {err}")


def determine_log_type(filename: str) -> str:
    """
    Determine the type of log based on the filename.

    We embed this as metadata in the Kafka message so the consumer knows
    which parser to use WITHOUT having to inspect the data itself.

    WHY: The consumer shouldn't need to "guess" how to parse a message.
    The producer knows the source, so it tags it. This is a form of the
    "Schema on Write" pattern — attaching metadata at ingestion time.
    """
    if "-dns" in filename:
        return "dns"
    elif "training" in filename or "testing" in filename or "validation" in filename:
        return "deep_kernel"
    else:
        return "standard_host"


def produce_file(producer: Producer, filepath: str):
    """
    Read a single CSV file in chunks and publish each row to Kafka.
    """
    filename = os.path.basename(filepath)
    log_type = determine_log_type(filename)
    rows_produced = 0

    for chunk in pd.read_csv(filepath, chunksize=10_000):
        for _, row in chunk.iterrows():
            # Build the message payload. This is the RAW row plus metadata.
            # We're not parsing/transforming — just packaging it for transport.
            message = {
                "source_file": filename,
                "log_type": log_type,
                "raw": row.to_dict(),
            }

            # produce() is non-blocking. It puts the message into an internal
            # buffer. The 'key' determines which partition this message goes to.
            # Same key = same partition = ordering guaranteed within that file.
            producer.produce(
                topic=KAFKA_TOPIC,
                key=filename,
                value=json.dumps(message),
                callback=delivery_report,
            )

            rows_produced += 1

            # poll(0) triggers delivery report callbacks for any messages that
            # have been acknowledged by the broker since the last poll.
            # Calling it periodically prevents the internal buffer from growing
            # unbounded (which would eventually cause a BufferError).
            if rows_produced % 10_000 == 0:
                producer.poll(0)
                print(f"  [{filename}] {rows_produced:,} rows queued...")

    return rows_produced


def main():
    # If a specific file is passed as an argument, use it.
    # Otherwise, ingest all CSVs. This lets us test with one small file first:
    #   python producer.py data/archive/labelled_2021may-ip-10-100-1-105-dns.csv
    if len(sys.argv) > 1:
        all_files = sys.argv[1:]
        for f in all_files:
            if not os.path.exists(f):
                print(f"File not found: {f}")
                sys.exit(1)
    else:
        data_glob = "data/archive/*.csv"
        all_files = sorted(glob.glob(data_glob))

    if not all_files:
        print(f"No files to ingest.")
        sys.exit(1)

    print(f"Found {len(all_files)} files to ingest.")

    # Create the producer. This establishes the connection to Confluent Cloud
    # using the config from config.py (SASL_SSL auth).
    producer = Producer(KAFKA_PRODUCER_CONFIG)

    total_rows = 0
    for filepath in all_files:
        print(f"\nProcessing: {os.path.basename(filepath)}")
        rows = produce_file(producer, filepath)
        total_rows += rows

    # flush() blocks until ALL messages in the internal buffer have been
    # delivered (or failed). Without this, your program might exit before
    # the last batch of messages is actually sent over the network.
    # The argument (30) is the timeout in seconds.
    remaining = producer.flush(30)
    if remaining > 0:
        print(f"\nWARNING: {remaining} messages were NOT delivered (timeout).")
    else:
        print(f"\nAll {total_rows:,} rows successfully published to '{KAFKA_TOPIC}'.")


if __name__ == "__main__":
    main()
