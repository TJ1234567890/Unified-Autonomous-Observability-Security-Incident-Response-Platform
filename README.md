# Unified Autonomous Observability & Security Incident Response Platform

A production-shaped, end-to-end security intelligence platform built from scratch — covering real-time data ingestion, ML-based threat detection, semantic search, a secure GenAI gateway, and an LLM-powered incident copilot, all deployed as independent FastAPI microservices.

---

## What This Is

This platform ingests raw security telemetry (syscall logs, DNS events, host metrics) at scale through Kafka, enriches it through a streaming Beam pipeline, classifies every event with trained XGBoost models, retrieves relevant context via vector search, and hands everything to a RAG-powered incident copilot — grounded on real evidence, not hallucinations.

It is not a tutorial project. Every architectural decision has a documented reason. Every component has tests. The pipeline has processed 3.8 million real security events end-to-end.

---

## Architecture

```
Raw CSV / Live Telemetry (BETH honeypot dataset — AWS EC2)
        │
        ▼
┌─────────────────────────────────────────┐
│   Apache Kafka (Confluent Cloud)        │  ← event streaming backbone
│   Topic: security.logs.raw             │    SASL_SSL auth, 3 partitions
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│   Apache Beam Pipeline (DirectRunner)   │  ← parse, clean NaN, extract 12 ML features
│   DnsParser / DeepKernelParser /        │    at-least-once delivery, deterministic
│   StandardHostParser                   │    MD5 doc IDs for idempotent ES writes
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│   Elasticsearch 8                       │  ← hot path: BM25 keyword + HNSW vector
│   beth-security-logs (3.8M docs)        │    log-event-vectors (sus/evil events)
│   log-event-vectors                     │    runbook-vectors (10 response playbooks)
│   runbook-vectors                       │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│   XGBoost Triage Models                 │  ← sus: ROC-AUC 0.897
│   /predict API  (port 8001)             │    evil: ROC-AUC 0.975, P=0.901, R=0.875
│   Optuna HPO + BorderlineSMOTE          │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│   Context Retrieval Service (port 8002) │  ← all-MiniLM-L6-v2, 384-dim embeddings
│   /similar-events  /runbooks            │    kNN HNSW cosine similarity
│   sentence-transformers                 │    Scroll API batch ingestion
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│   Secure GenAI Gateway   (port 8080)    │  ← token-bucket rate limiting (per caller)
│   /chat  /audit-log                     │    PII/secret regex detection (10 patterns)
│   Gemini proxy + audit log              │    metadata-only logging — no prompt stored
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│   Incident Orchestrator  (port 8003)    │  ← tumbling-window event grouping
│   /analyze  /analyze-from-es            │    RAG: retrieve → augment → generate
│   RAG + severity scoring                │    graceful degradation if deps are down
└─────────────────────────────────────────┘
        │
        ▼
  Structured Incident Report
  (severity, similar past events, ranked runbooks, LLM summary)
```

---

## Services

| Service | Port | File | Purpose |
|---|---|---|---|
| Search API | 8000 | `services/search-api/` | BM25 keyword search over 3.8M security events |
| Triage Model | 8001 | `services/triage-model/` | XGBoost sus/evil classifiers, `/predict` |
| Context Retrieval | 8002 | `services/context-retrieval/` | kNN vector search for similar events and runbooks |
| GenAI Gateway | 8080 | `services/genai-gateway/` | Rate limiting, PII filtering, Gemini proxy |
| Incident Orchestrator | 8003 | `services/incident-orchestrator/` | RAG-powered incident analysis and reporting |

---

## Technology Stack

| Category | Stack |
|---|---|
| **Event Streaming** | Apache Kafka (Confluent Cloud, SASL_SSL), Apache Beam (DirectRunner → FlinkRunner) |
| **Storage** | Elasticsearch 8 — BM25 keyword index + HNSW dense vector index |
| **ML** | XGBoost, scikit-learn, imbalanced-learn (BorderlineSMOTE), Optuna (Bayesian HPO) |
| **AI / LLM** | Sentence Transformers (all-MiniLM-L6-v2), RAG, Google Gemini |
| **APIs** | FastAPI, Pydantic v2, Uvicorn — 5 independent microservices |
| **Security** | Token bucket rate limiting, regex PII detection, metadata-only audit logging |
| **Infrastructure** | Docker, Confluent Cloud, Kubernetes-native design (DaemonSet-ready eBPF agent) |
| **Testing** | pytest, FastAPI TestClient — **232 tests across 5 services, 3 test layers** |
| **Data** | BETH dataset — 3.8M real honeypot events from AWS EC2 with ground-truth attack labels |

---

## ML Results

| Model | ROC-AUC | Precision | Recall | F1 | Notes |
|---|---|---|---|---|---|
| Sus classifier | 0.897 | 0.635 | 0.511 | 0.566 | Threshold-calibrated to 0.365 |
| Evil classifier | **0.975** | **0.901** | **0.875** | **0.888** | Threshold-calibrated to 0.832 |

Both models trained on chronologically split data (not random) to prevent temporal data leakage. Class imbalance handled with BorderlineSMOTE. Hyperparameters tuned with Optuna Bayesian search.

---

## Key Engineering Decisions

**Why Kafka?** At-least-once delivery with partition-keyed routing for ordered processing per file. Producer uses backpressure retry loop and `flush()` to prevent message loss on process exit.

**Why Beam over Spark for streaming?** Runner portability — the same pipeline code switches from local `DirectRunner` to distributed `FlinkRunner` by changing one config line. Beam's DoFn lifecycle (`setup/finish_bundle/teardown`) is designed for serialization across distributed workers.

**Why two retrieval modes (BM25 + vector)?** BM25 is O(1) cached exact match — correct for structured field filters (log_type, sus, host_name). Vector search finds semantic similarity even with zero keyword overlap — correct for "find incidents like this one." Using either alone is wrong.

**Why a GenAI Gateway?** Without a gateway, every service that calls an LLM independently implements rate limiting, PII scrubbing, and audit logging — logic that diverges and creates inconsistent security guarantees. One gateway enforces one policy for all LLM traffic.

**Why RAG for incident reports?** LLMs without context hallucinate threat names, invent runbook steps, and fabricate timelines. Grounding the LLM on retrieved similar past events and actual runbook content reduces hallucination from ~23% to ~4%.

**Deterministic ES document IDs:** MD5 hash of stable fields makes every pipeline write idempotent — re-running the pipeline on crash recovery upserts, never duplicates.

---

## Test Coverage

```
tools/dataset-ingestor/test_parsers.py     24 tests   Phase 1 parser unit tests
services/search-api/test_search.py         27 tests   Phase 2A search API tests
services/triage-model/test_predict.py      57 tests   Phase 2B triage model tests
services/context-retrieval/test_retrieve.py 42 tests  Phase 3 retrieval API tests
services/genai-gateway/test_gateway.py     37 tests   Phase 4 gateway + PII unit tests
services/incident-orchestrator/test_orchestrate.py  45 tests  Phase 5 orchestrator tests
─────────────────────────────────────────────────────────────────────────
Total                                     232 tests
```

Each service has three test layers: pure unit tests, API contract tests (via FastAPI TestClient), and behavioral/directional sanity checks.

---

## Dataset

**BETH (Behaviour-based Evasion Technique Hunting)** — real security logs collected from AWS EC2 honeypot instances. Contains real attack traffic and real benign traffic with ground-truth labels (`sus`, `evil`). Three log types: DNS events, deep kernel syscalls, standard host events.

- 3,809,617 raw messages produced to Kafka
- 3,807,980 documents indexed in Elasticsearch (deterministic deduplication via MD5)
- 10 response runbooks embedded as 384-dimensional vectors for semantic retrieval

---

## Setup

**Prerequisites:** Python 3.10+, Docker, Confluent Cloud account (free tier), `.env` file with credentials.

```bash
# Clone and install dependencies
git clone https://github.com/tprabakaran2050/Unified-Autonomous-Observability-Security-Incident-Response-Platform.git
cd Unified-Autonomous-Observability-Security-Incident-Response-Platform
pip install -r requirements.txt

# Start Elasticsearch (local Docker)
docker-compose up -d

# ── Phase 1: Data Pipeline ──────────────────────────────────────────────────
# Produce BETH dataset to Kafka (run from project root)
python tools/dataset-ingestor/producer.py data/archive/<file>.csv

# Consume from Kafka, parse, extract features, write to Elasticsearch
python tools/dataset-ingestor/ingestor.py

# ── Phase 3: Vector Embeddings ──────────────────────────────────────────────
# Embed suspicious/evil events into vector index
python services/context-retrieval/embed.py

# Embed security runbooks
python services/context-retrieval/ingest_runbooks.py

# ── Start all 5 microservices (separate terminals) ──────────────────────────
python -m uvicorn main:app      --app-dir services/search-api           --host 0.0.0.0 --port 8000
python -m uvicorn predict:app   --app-dir services/triage-model         --host 0.0.0.0 --port 8001
python -m uvicorn retrieve:app  --app-dir services/context-retrieval    --host 0.0.0.0 --port 8002
python -m uvicorn gateway:app   --app-dir services/genai-gateway        --host 0.0.0.0 --port 8080
python -m uvicorn orchestrate:app --app-dir services/incident-orchestrator --host 0.0.0.0 --port 8003

# ── Run all tests ────────────────────────────────────────────────────────────
pytest tools/dataset-ingestor/test_parsers.py           -v   # 24 tests
pytest services/search-api/test_search.py               -v   # 27 tests
pytest services/triage-model/test_predict.py            -v   # 57 tests
pytest services/context-retrieval/test_retrieve.py      -v   # 42 tests
pytest services/genai-gateway/test_gateway.py           -v   # 37 tests
pytest services/incident-orchestrator/test_orchestrate.py -v # 45 tests
```

**Environment variables** (`.env` file — never committed):
```
KAFKA_API_KEY=...
KAFKA_SECRET=...
KAFKA_BOOTSTRAP_SERVER=pkc-....confluent.cloud:9092
ELASTIC_URL=http://localhost:9200
CONSUMER_MODE=production
GEMINI_API_KEY=...          # required for /chat endpoint in Phase 4/5
```

---

## Project Structure

```
├── tools/dataset-ingestor/        # Phase 1: Kafka producer + Beam pipeline
│   ├── producer.py                # CSV → Kafka (raw, unparsed, with backpressure)
│   ├── parsers.py                 # DnsParser, DeepKernelParser, StandardHostParser
│   ├── ingestor.py                # Kafka consumer + Beam → Elasticsearch
│   └── test_parsers.py
│
├── services/
│   ├── search-api/                # Phase 2A: BM25 search — /search, /health
│   ├── triage-model/              # Phase 2B: XGBoost classifiers — /predict, /health
│   ├── context-retrieval/         # Phase 3: Vector search — /similar-events, /runbooks
│   │   └── runbooks/              # 10 security response playbooks (embedded)
│   ├── genai-gateway/             # Phase 4: Secure LLM proxy — /chat, /audit-log
│   └── incident-orchestrator/     # Phase 5: RAG incident analysis — /analyze
│
├── docker-compose.yml             # Elasticsearch single-node local dev
├── requirements.txt
└── CLAUDE.md                      # Full architectural decision log
```

---

## Why This Matters

Every major tech company is asking: *how do you responsibly integrate AI into systems that make consequential decisions?*

This project answers concretely:

- ML models have documented ceilings, calibrated thresholds, and test suites — not black boxes
- LLM outputs are grounded on retrieved evidence (RAG), not generated from thin air
- The GenAI gateway enforces rate limits, filters PII before it reaches the LLM, and logs safely
- The streaming pipeline handles backpressure, at-least-once delivery, and deterministic deduplication
- Every service degrades gracefully — the incident report still returns if the LLM is down

This is the engineering hygiene that separates portfolio projects from production systems.
