"""
Shared configuration for the dataset ingestor pipeline.

WHY THIS EXISTS:
Both the producer (writes to Kafka) and the consumer (reads from Kafka, writes to ES)
need the same connection details. Instead of duplicating .env loading in every file,
we centralize it here. This is the "Single Source of Truth" principle — if the Kafka
cluster changes, you change ONE file.
"""

import os
from dotenv import load_dotenv

# load_dotenv() reads your .env file and sets environment variables.
# This means your secrets never appear as string literals in source code.
# Anyone who clones the repo gets an empty shell — they provide their own .env.
load_dotenv()

"""
KAFKA_API_KEY = CK4Z6ZR4QT2KF2RT
KAFKA_SECRET = cfltu2sMmztFr5Ap4KegecG2363v4efNAnzZsAppJP4JiCboQtl0ipu5K4eXWI1g
KAFKA_BOOTSTRAP_SERVER = pkc-619z3.us-east1.gcp.confluent.cloud:9092
KAFKA_RESOURCES = lkc-2zq5w2

ELASTIC_API_KEY = eUgwTmdac0JuWWxFRGtndVpSelE6OUVFR0N3MEdtMXdWdHZMb1RHZFpBUQ==
ELASTIC_URL = http://localhost:9200

# CONSUMER_MODE controls how the Kafka consumer reads messages.
# "dev"        — assign() at OFFSET_BEGINNING, always reads all data fresh (local testing)
# "production" — subscribe() via consumer group, resumes from last committed offset (multi-worker)
CONSUMER_MODE = production
"""

# --- Kafka Configuration (Confluent Cloud) ---

KAFKA_BOOTSTRAP_SERVER = os.getenv("KAFKA_BOOTSTRAP_SERVER")
KAFKA_API_KEY = os.getenv("KAFKA_API_KEY")
KAFKA_SECRET = os.getenv("KAFKA_SECRET")

# This is the topic name where raw security logs will land.
# Convention: <domain>.<data-type>.<stage>
# "security" = domain, "logs" = what it is, "raw" = unprocessed
KAFKA_TOPIC = "security.logs.raw"

# The producer config dict that confluent-kafka expects.
# SASL_SSL = authentication (SASL) over encrypted connection (SSL).
# PLAIN = the SASL mechanism — username/password style. Confluent Cloud uses this.
KAFKA_PRODUCER_CONFIG = {
    "bootstrap.servers": KAFKA_BOOTSTRAP_SERVER,
    "security.protocol": "SASL_SSL",
    "sasl.mechanisms": "PLAIN",
    "sasl.username": KAFKA_API_KEY,
    "sasl.password": KAFKA_SECRET,
}

# --- Elasticsearch Configuration ---

ELASTIC_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
ES_INDEX_NAME = "beth-security-logs"
