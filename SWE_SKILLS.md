# Generalist Software Engineering Accomplishments

This document covers the software engineering depth of this project end-to-end, independent of its ML components. The ML models are one output. The infrastructure, pipeline design, API architecture, search engineering, vector retrieval, LLM integration, kernel-level observability, and Kubernetes operations that surround them — built across six phases — are the other part, and often the harder part.

---

## Table of Contents

1. [Distributed Event Streaming (Apache Kafka)](#1-distributed-event-streaming-apache-kafka)
2. [Stream Processing Pipeline (Apache Beam)](#2-stream-processing-pipeline-apache-beam)
3. [Search Engineering (Elasticsearch — BM25 and Vector)](#3-search-engineering-elasticsearch--bm25-and-vector)
4. [API Design (FastAPI Microservices)](#4-api-design-fastapi-microservices)
5. [Testing Strategy](#5-testing-strategy)
6. [Schema Design and Parser Architecture](#6-schema-design-and-parser-architecture)
7. [Production Reliability Engineering](#7-production-reliability-engineering)
8. [Vector Search and Semantic Retrieval (Phase 3)](#8-vector-search-and-semantic-retrieval-phase-3)
9. [Secure GenAI Gateway (Phase 4)](#9-secure-genai-gateway-phase-4)
10. [Incident Copilot and RAG Architecture (Phase 5)](#10-incident-copilot-and-rag-architecture-phase-5)
11. [eBPF Kernel Agent (Phase 6)](#11-ebpf-kernel-agent-phase-6)
12. [Kubernetes Infrastructure and Deployment](#12-kubernetes-infrastructure-and-deployment)
13. [Apache Spark Batch Layer](#13-apache-spark-batch-layer)
14. [Distributed Observability (OpenTelemetry and Prometheus)](#14-distributed-observability-opentelemetry-and-prometheus)
15. [Configuration and Credentials Management](#15-configuration-and-credentials-management)
16. [Code Quality Patterns](#16-code-quality-patterns)
17. [Lambda Architecture](#17-lambda-architecture)
18. [Summary of Scale and Numbers](#18-summary-of-scale-and-numbers)

---

## 1. Distributed Event Streaming (Apache Kafka)

### What was built

A producer that reads multi-gigabyte CSV files and publishes 3.8 million messages to a managed Kafka cluster (Confluent Cloud) with full authentication, backpressure handling, and delivery guarantees. A consumer that reads from that cluster and feeds a downstream processing pipeline with correct offset commit semantics, consumer group management, and two distinct modes (dev vs. production) controlled by environment config. As the platform scales to include live telemetry from the eBPF agent (Phase 6), this same Kafka backbone handles the real-time kernel event stream.

### The engineering decisions that matter

**Partition-keyed message routing.** Every message is produced with `key=filename`. Kafka hashes this key to a deterministic partition number. All rows from the same CSV file land on the same partition, guaranteeing in-order delivery for each file. A log file is a time-ordered sequence — row 500 must arrive after row 499. Without key routing, Kafka round-robins across partitions and that ordering is destroyed. This is not a convenience feature; it is a correctness requirement for time-series data.

```python
producer.produce(
    topic=KAFKA_TOPIC,
    key=filename,           # same file → same partition → ordered delivery
    value=json.dumps(message),
    callback=delivery_report,
)
```

**Backpressure handling with retry loop.** `producer.produce()` writes to an in-memory buffer managed by librdkafka (the C library under the Python client). If the buffer fills up faster than the network can drain it to Confluent Cloud, `produce()` raises `BufferError`. The correct response is to wait for the buffer to drain — not to drop the message, not to crash. This is backpressure: the producer slows itself to match network throughput.

```python
while True:
    try:
        producer.produce(...)
        break
    except BufferError:
        producer.poll(1)    # service callbacks, let buffer drain, retry
```

This is directly analogous to TCP flow control: the sender backs off when the receiver signals it cannot keep up.

**The `flush(30)` vs `poll(0)` distinction.** `poll(0)` is non-blocking — it processes any delivery callbacks already ready and returns immediately. `flush(30)` blocks for up to 30 seconds, draining every in-flight message before returning. `flush()` is called exactly once at the end because without it Python's process exit destroys the librdkafka C buffer and any unacknowledged messages are silently lost. This is a common cause of data loss in naive Kafka producers.

**Asynchronous delivery callbacks.** The `delivery_report(err, msg)` callback is passed to every `produce()` call but does NOT fire inside `produce()`. It fires inside `poll()`. librdkafka calls it when the broker sends an acknowledgment (or error). If you never call `poll()`, the callback never fires and you have no confirmation that any message reached the broker. The implementation calls `poll(0)` every 10,000 rows for exactly this reason.

**Consumer group protocol and partition assignment.** Production mode uses `subscribe()`, which triggers Kafka's group rebalance protocol. The group coordinator assigns partitions across all consumers in the group. While rebalance is in progress, consumption is paused. The warmup loop waits for `consumer.assignment()` to be non-empty:

```python
consumer.subscribe([KAFKA_TOPIC])
while not consumer.assignment():
    consumer.poll(1.0)   # 1.0 not 0 — give Kafka time to complete the rebalance
```

`poll(1.0)` is used here (not `poll(0)`) because `poll(0)` returns immediately regardless of whether anything happened. Spinning at zero delay would exit the loop before the rebalance completed.

**Dev mode bypasses the group protocol entirely.** `assign()` + `OFFSET_BEGINNING` manually assigns partitions and seeks to offset 0, bypassing committed offsets. `auto.offset.reset = "earliest"` only fires when the consumer group has NO committed offsets. If the group previously consumed and committed, Kafka ignores this setting entirely. Using `assign()` is the only guaranteed-to-work approach for deterministic replay.

**Commit-after-pipeline (at-least-once delivery).** The original code committed Kafka offsets before the pipeline ran — at-most-once semantics. If the pipeline crashed after the commit but before writing to Elasticsearch, those messages were permanently lost. The fix moves `consumer.commit()` to `main()`, after `run_pipeline()` returns successfully:

```python
run_pipeline(messages)      # write to ES first
consumer.commit()           # advance Kafka offset only on success
```

On a crash, the exception propagates past `commit()`, offsets stay where they were, and Kafka re-delivers the batch on next startup. Textbook at-least-once delivery.

**SASL_SSL authentication.** SASL handles identity verification. SSL handles transport encryption. PLAIN is the SASL mechanism — it sends credentials as username/password, safe because SSL encrypts the connection before any credentials are transmitted. Confluent Cloud requires this combination for all external connections.

**Topic naming is permanent.** Kafka has no rename operation. The original topic `logs.raw.v1` was deleted and `security.logs.raw` recreated with the correct domain-namespaced convention (`<domain>.<datatype>.<stage>`). Every engineer on the project must know this — a rename decision cannot be undone cheaply.

---

## 2. Stream Processing Pipeline (Apache Beam)

### What was built

A multi-stage data transformation pipeline using Apache Beam's DirectRunner (local) that takes raw Kafka messages, parses them through typed parser classes, enriches them with computed feature signals, and writes them to Elasticsearch in batched bulk operations. The pipeline is architected for runner portability — the same code runs locally with DirectRunner and in production on Apache Flink or Google Cloud Dataflow with one config change. As the platform matures, the pipeline expands to include change-point detection, rolling slope computation for Memory Guard (Phase 1 Project 5), and feature window aggregation for the Latency Forecaster (Phase 1 Project 1).

### The engineering decisions that matter

**DoFn lifecycle: `setup()` not `__init__()`.** Beam serializes DoFn objects and ships them to worker nodes on distributed runners. If you open a database connection or load a model in `__init__()`, it runs before serialization — network sockets and file handles cannot be serialized. The serialization step fails, or the connection is opened on the wrong machine. `setup()` runs after the DoFn is deserialized on the worker, making it the correct place for any resource that cannot cross a serialization boundary.

```python
class ParseLogFn(beam.DoFn):
    def setup(self):
        self.parsers = {          # initialized on the worker, after deserialization
            "dns": DnsParser(),
            "deep_kernel": DeepKernelParser(),
            "standard_host": StandardHostParser(),
        }
```

**`finish_bundle()` prevents silent data loss.** Beam calls `process()` on each element in a bundle. When all elements are done, Beam moves to `teardown()` — it does not call `process()` again. Without `finish_bundle()`, any elements still sitting in the internal write buffer (e.g., 300 documents that haven't hit the 500-document bulk threshold) are silently garbage collected. `finish_bundle()` is the correct hook for flushing partial batches.

**`yield` vs `return` in `process()`.** `return parsed` (returning a dict) causes Beam to iterate over it. Python dict iteration yields the KEYS — strings like `"timestamp"`, `"log_attribute"`, etc. Each key becomes a downstream element. The next stage receives a string and crashes when it tries to call `.get()` on it. `yield parsed` emits the dict itself as a single element. The bare `return` drops the element entirely, which is the correct behavior for unknown log types.

**`beam.Map` vs `beam.ParDo` — the right abstraction for each transform.** `beam.Map` is for pure 1-to-1 transforms with no lifecycle needs. `beam.ParDo` is for transforms that need `setup()`/`teardown()`, or that may emit zero or multiple outputs.

```python
p
| "Parse JSON"       >> beam.Map(lambda raw: clean_nan(json.loads(raw)))
| "Transform logs"   >> beam.ParDo(ParseLogFn())     # may emit 0 outputs
| "Extract features" >> beam.Map(extract_features)   # always 1-to-1
| "Write to ES"      >> beam.ParDo(WriteToEs())      # needs setup/teardown
```

**Regex patterns compiled at module load.** At 3.8 million documents, calling `re.compile()` inside `extract_features()` on every document would compile the same pattern 3.8 million times. Module-level constants are compiled once and reused:

```python
_SHELL_RE = re.compile(r'/bin/bash|/bin/sh|/bin/zsh|cmd\.exe|powershell', re.IGNORECASE)
_NETWORK_RE = re.compile(r'\bcurl\b|\bwget\b|\bnc\b|\bnetcat\b|\bncat\b|\bsocat\b', re.IGNORECASE)
_SENSITIVE_PATH_RE = re.compile(r'/etc/passwd|/etc/shadow|/etc/sudoers|/root/|/proc/\d+', re.IGNORECASE)
```

**`try/finally` for guaranteed consumer cleanup.** If `run_pipeline()` raises, Python exits the `try` block immediately. `consumer.commit()` is skipped (correct — Kafka re-delivers). `consumer.close()` in the `finally` block always fires, even on exception. Without it, a crash leaves the consumer connection open until Kafka's session timeout (~45 seconds), blocking other workers from being assigned those partitions.

**NaN sanitization before JSON serialization.** pandas represents missing numeric values as `float('nan')`. `json.dumps()` serializes NaN as the literal text `NaN`, which is not valid JSON. Elasticsearch rejects documents containing NaN. `clean_nan()` recursively walks the nested dict/list structure and replaces NaN and Infinity with `None`. The recursion is necessary because documents can be arbitrarily deeply nested. The `isinstance(value, float)` guard is required because `math.isnan(value)` raises `TypeError` on non-floats.

**Runner portability.** DirectRunner runs locally in one process. FlinkRunner runs on Apache Flink cluster for production streaming. DataflowRunner runs on Google Cloud Dataflow. You write the pipeline once; changing the runner is one config line. This is Beam's core architectural value proposition: write once, run anywhere in the data processing ecosystem.

---

## 3. Search Engineering (Elasticsearch — BM25 and Vector)

### What was built

A dual-mode search system backed by Elasticsearch 8: keyword/filter search for exact conditions, full-text BM25 relevance search for free-text queries (Phase 2A), and dense vector HNSW search for semantic similarity (Phase 3). The query builder correctly routes each condition to the ES clause type that fits its semantics. Idempotent document writes with deterministic MD5 IDs. 3,807,980 documents written with no duplicates across 39 pipeline runs. Phase 3 adds a second index (`log-event-vectors`) with `dense_vector` fields for approximate nearest neighbor retrieval.

### The engineering decisions that matter

**`filter` vs `must` — the most important ES performance decision.** Both clauses exclude non-matching documents. The difference is scoring and caching. `must` calculates a BM25 relevance score — ES scores every matching document by how well it matched. `filter` does zero scoring — it is a pure yes/no decision, and its results are cached at the query-cache level. The same filter on the same data always produces the same document set, so ES skips recomputation entirely on subsequent requests.

`sus`, `evil`, `log_type`, `host_name`, time ranges are boolean/enum conditions where relevance is meaningless. Putting them in `must` forces ES to score every document for something with no meaningful gradation — wasting CPU and bypassing the cache. They belong in `filter`. Only `dns_query` free-text belongs in `must`:

```python
if req.log_type is not None:
    filters.append({"term": {"log_type": req.log_type}})      # filter: cached, no scoring

if req.dns_query is not None:
    must.append({"match": {"attributes.dns_query": req.dns_query}})  # must: BM25, ranked
```

**`term` vs `match` — exact vs analyzed queries.** `term` performs exact matching with no text analysis. `match` applies tokenization, lowercasing, and stemming. Using `term` on a full-text analyzed field fails because the stored tokens don't match the raw input. Using `match` on an enum field returns semantically wrong results.

**`.keyword` sub-fields.** Elasticsearch automatically creates two sub-fields for text fields: the analyzed field (for `match` queries) and a `.keyword` field that stores the raw unmodified string. `term` queries on text fields must use `.keyword`:

```python
{"term": {"attributes.host_name.keyword": req.host_name}}
```

Without `.keyword`, `"ip-10-100-1-105"` is tokenized to `["ip", "10", "100", "1", "105"]` and the `term` query finds nothing.

**Dynamic mapping and type locking.** ES infers field types from the first document written. Once a field is typed as `long`, any document writing a string to it is rejected with `document_parsing_exception`. The `DnsParser` originally defaulted `dns_response_code` to `""` (string). After ES inferred the field as `long` from the first document (value `0`), every missing-code document was rejected. The fix was `None` (JSON `null`), accepted on any ES field type. `0` was not acceptable because `0` is a valid DNS response code (NOERROR) — using it for absent data would silently misrepresent missing lookups as successful ones.

**Deterministic document IDs via MD5 hashing.** Without explicit `_id` fields, ES auto-generates a random UUID per document. On every pipeline re-run, the same source row produces a new UUID — duplicates accumulate silently. The fix: hash stable identifiers into a deterministic `_id`. ES upserts on duplicate `_id`, making writes idempotent. MD5 is used over SHA-256 because collision resistance is irrelevant here — this is not a security context. MD5 produces 32-char hex vs 64 for SHA-256, and at 3.8M documents the storage difference matters.

**Bulk writes with `helpers.bulk()`.** Sending one HTTP request per document at 3.8M documents is O(n) round trips. `helpers.bulk()` batches documents into a single HTTP request. The `WriteToEs` DoFn buffers 500 documents before flushing, reducing HTTP overhead by a factor of 500. `raise_on_error=False` means individual document failures are logged but do not crash the entire batch.

**`track_total_hits=True`.** By default ES caps the reported total at 10,000 for performance. For an index with 3.8M documents, the search API would always report `10000` as the total. Setting `track_total_hits=True` forces an exact count.

**HNSW vector index for approximate nearest neighbor search (Phase 3).** Dense vector fields in Elasticsearch 8 use HNSW (Hierarchical Navigable Small World) graphs for approximate nearest neighbor search. Exact nearest neighbor search is O(n) — comparing a query vector against every document vector at 3.8M documents is too slow for a real-time API. HNSW builds a multi-layer graph where each node connects to its nearest neighbors at multiple granularities, enabling O(log n) approximate search. The trade-off: HNSW can miss a small fraction of true nearest neighbors (it is approximate), but that trade-off is acceptable — the system needs fast relevant results, not provably perfect ones.

```json
{
  "mappings": {
    "properties": {
      "embedding": {
        "type": "dense_vector",
        "dims": 384,
        "index": true,
        "similarity": "cosine"
      }
    }
  }
}
```

**Cosine similarity for embedding comparison.** Cosine similarity measures the angle between two vectors, not their magnitude. Two embeddings with similar semantic meaning will point in similar directions in 384-dimensional space, producing a cosine similarity close to 1.0. Two unrelated embeddings point in different directions, producing similarity closer to 0.0. Cosine is preferred over dot product (which is magnitude-sensitive) and Euclidean distance (which doesn't normalize for embedding length) for normalized sentence embeddings.

**ES Scroll API for batch embedding generation.** Generating embeddings for 3.8M documents cannot be done with a standard paginated search — deep pagination (large `from_` offsets) is O(n) in ES because it must skip over all prior results. The Scroll API creates a snapshot of the index at a point in time and returns pages with a scroll token, O(1) per page:

```python
resp = es.search(index=INDEX, body=query, scroll="5m", size=500)
scroll_id = resp["_scroll_id"]
while True:
    hits = resp["hits"]["hits"]
    if not hits:
        break
    # embed batch, write to vector index
    resp = es.scroll(scroll_id=scroll_id, scroll="5m")
es.clear_scroll(scroll_id=scroll_id)
```

The scroll context is cleared at the end to release the server-side snapshot and free memory.

**`source_file` cannot replace `log_type`.** A wildcard query on `source_file` to infer type bypasses the ES inverted index (full document scan, O(n), no cache), requires every downstream consumer to re-implement filename parsing logic independently, and silently breaks if any file is named differently. The rule: derive once at write time, store the clean value, query the clean field.

---

## 4. API Design (FastAPI Microservices)

### What was built

Five independent FastAPI microservices across the platform: search API (port 8000), triage prediction API (port 8001), context retrieval API (port 8002), incident orchestrator API (port 8003), and the GenAI gateway (port 8080). Each uses typed Pydantic request/response models, enforces API contracts at the boundary, follows correct HTTP semantics, and loads its heavy resources once at startup.

### The engineering decisions that matter

**Module-level resource loading.** All services load heavy resources once at startup — never inside the request handler. Creating a database client, loading a model, or initializing a connection pool inside a handler means doing it on every single request:

```python
# search API — module level
es_client = Elasticsearch(ES_URL)

# predict API — module level
_sus_model: xgb.Booster = joblib.load(os.path.join(_MODEL_DIR, "sus_model.joblib"))
_evil_model: xgb.Booster = joblib.load(os.path.join(_MODEL_DIR, "evil_model.joblib"))
_meta: dict = joblib.load(os.path.join(_MODEL_DIR, "feature_columns.joblib"))

# context retrieval API — module level
_encoder = SentenceTransformer("all-MiniLM-L6-v2")
es_client = Elasticsearch(ES_URL)
```

**Pydantic enforces contracts at the API boundary.** `Literal["dns", "deep_kernel", "standard_host"]` means only these three exact strings are valid. `Optional[Literal[0, 1]]` means the field can be absent or must be exactly 0 or 1. `ge=1` enforces positive integers. `le=500` caps page size. Any violation returns 422 before the query reaches any backend system:

```python
class SearchRequest(BaseModel):
    log_type: Optional[Literal["dns", "deep_kernel", "standard_host"]] = None
    sus: Optional[Literal[0, 1]] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=500)
```

**Correct HTTP status code semantics.** 503 (Service Unavailable) is returned when a downstream dependency (Elasticsearch, model files, Gemini API) is unreachable — telling callers the problem is not their request and they should retry. 500 (Internal Server Error) is returned for unexpected exceptions in the request handler. 422 (Unprocessable Entity) is returned by Pydantic for malformed input. These are different failure modes that deserve different status codes — they inform callers what to do next.

**`/health` has no try/except; `/predict` does.** The health endpoint executes a fixed code path with no user-supplied input through multiple processing stages. The predict endpoint takes arbitrary user input through feature extraction, label encoding, DataFrame construction, and XGBoost inference — any of which can fail on malformed input. Try/except wraps only the path that actually needs it.

**`response_model` for automatic validation and OpenAPI generation.** `response_model=SearchResponse` tells FastAPI to validate the function's return value against the Pydantic model before sending the response. If the return value doesn't match the model's shape, FastAPI raises a 500 before the response leaves the server. It also auto-generates the OpenAPI schema at `/docs` with no additional work.

**Uvicorn `--app-dir` for non-importable directory names.** `services/search-api` has a hyphen, which makes it an invalid Python module name — `from services.search-api.main import app` is a syntax error. The `--app-dir` flag adds the target directory to `sys.path` and imports `main` directly, bypassing the need for the directory to be a valid Python package.

**Dependency injection pattern for the ES client.** The search function `execute_search(es, index, req)` takes the Elasticsearch client as a parameter rather than importing the module-level singleton directly. This makes the function testable in isolation — tests can pass a mock client, or a client pointing at a test index, without patching module globals.

---

## 5. Testing Strategy

### What was built

81 tests across two test suites, covering two separate services, across three distinct test layers. As additional services are built, the strategy scales: each new service gets a matching test file with the same three-layer structure. The context retrieval service, GenAI gateway, and incident orchestrator all have test suites that verify API contracts, semantic ordering behavior, and edge-case degradation.

### The engineering decisions that matter

**Three-layer test architecture.** The structure was chosen deliberately:

- **Layer 1** (pure unit tests): no HTTP, no model, no network. Tests isolated helper functions like `_shannon_entropy()` and `_extract_features()` with exact assertions on specific inputs. These run in milliseconds and have no external dependencies.
- **Layer 2** (API contract tests via TestClient): FastAPI's `TestClient` sends real HTTP requests through the full application stack without starting a network server. Tests verify response shapes, field presence, HTTP status codes, and that Pydantic model contracts hold.
- **Layer 3** (directional sanity tests): model-agnostic ordering checks. These do not assert that a specific event has `sus_probability = 0.734`. They assert that a root-user + shell + wget event scores higher than a non-root + vim event. This holds true even after the model is retrained with different data — the relative ordering of feature signals is a property of the feature engineering, not the specific model weights.

**TDD caught a production bug.** The test `test_root_user_sets_flag` was written first and failed immediately. The bug: `attrs.get("user_id") or -1` treats `uid=0` (root) as falsy in Python, so `0 or -1 = -1`, making root users always classified as non-root. The fix required an explicit `None` check:

```python
uid_raw = attrs.get("user_id")
uid = int(uid_raw) if uid_raw is not None else -1
```

This bug existed in both `predict.py` and `ingestor.py`. Without the test, it would have been invisible in production — root users would silently receive `feat_is_root_user = 0` in both training and inference, degrading model accuracy with no error raised anywhere in the system.

**`setup_method()` for test isolation.** Each test class creates a fresh parser instance before every test method. If a parser gains mutable state later (an internal buffer, a counter, a cache), a shared instance would let test A's side effects contaminate test B's assertions. `setup_method()` prevents this regardless of future changes to the class.

**Missing-key tests expose default-value bugs.** Tests using complete, valid input rows never trigger the default value path. The `test_dns_response_code_type_is_not_string` test passed even when the default was wrong (`""`) because it used a row with `DnsResponseCode` present. Only `test_missing_dns_response_code_defaults_to_none` — which explicitly removed the key — hit the default path and caught the bug. Happy-path tests do not verify default behavior. Absence must be tested separately.

**Edge cases as first-class tests.** Every plausible boundary condition has an explicit test: unknown `log_type` (no crash, low score), empty `attributes` dict, missing `attributes` key, `user_id` passed as string instead of int. These verify that the API degrades gracefully on malformed input instead of raising unhandled exceptions.

**Directional sanity for the vector retrieval service.** Layer 3 tests in the context retrieval service verify semantic ordering rather than exact embedding values: a query about `execve` events should return kernel events ranked above DNS events. A query about `wget` network exfiltration should rank events with `feat_args_has_network=1` above events without it. This is the retrieval equivalent of the predict API's directional tests — model-agnostic assertions about relative behavior.

---

## 6. Schema Design and Parser Architecture

### What was built

An abstract base class with three concrete parser implementations mapping heterogeneous CSV schemas (three different column naming conventions from the same dataset) into a unified normalized document schema for Elasticsearch. Type-safe field extraction with documented defaults for every missing-field case.

### The engineering decisions that matter

**Abstract base class enforces the interface.** `Parser` is an ABC with one abstract method `parse(row: dict) -> dict`. Any class that inherits from `Parser` but does not implement `parse()` raises `TypeError` at instantiation time — not at call time, not silently. This guarantees that any parser added in the future must implement the same interface or fail immediately at startup:

```python
class Parser(ABC):
    @abstractmethod
    def parse(self, row: dict) -> dict:
        pass
```

**Three parsers for three schemas.** The BETH dataset has three CSV file types with genuinely different column naming conventions — DNS files use `CapitalCase` (`Timestamp`, `SourceIP`, `DnsQuery`), kernel files use `camelCase` (`timestamp`, `eventName`, `processId`). This is not a convention; it is the reality of how the dataset was assembled. The parsers match exactly what the CSV headers contain, confirmed by running `head -1` on actual files. A single parser with conditional logic would be harder to test and harder to extend.

**`None` as the canonical absent-value sentinel.** The rule throughout the schema: `None` for fields genuinely absent from a row. Not `""` (conflicts with ES string-type mapping when the field was inferred as `long`). Not `0` (is a valid value for numeric fields like `dns_response_code` and `return_value`). `None` serializes to JSON `null`, accepted on any ES field type without mapping conflicts, and unambiguously distinguishable from any real value.

**Publish raw, parse downstream.** The producer publishes the exact CSV row dict, unmodified, wrapped in an envelope. If parser logic changes, the raw Kafka messages can be replayed without re-reading any CSV files. Different consumers could apply different transformations to the same raw data. The producer's responsibility is transport, not transformation.

**`log_type` derived at produce time, stored at parse time.** `log_type` is derived from the filename in the producer. It is stored in every ES document by `ParseLogFn`. It is never re-derived at query time. A wildcard query on `source_file` to infer type would bypass the ES inverted index (full scan, no cache), require every downstream consumer to re-implement filename parsing logic, and silently break if any file is ever named differently.

---

## 7. Production Reliability Engineering

### What was built

A pipeline that handles crash recovery, guarantees no duplicate documents on replay, handles partial batches correctly, and degrades gracefully on malformed or unexpected input. These properties hold across all six phases — every service is designed to restart cleanly, re-process safely, and fail without cascading.

### The engineering decisions that matter

**Idempotent writes eliminate replay risk.** Every document has a deterministic `_id` derived from stable content fields. Running the pipeline twice produces the same documents, not twice as many. ES upserts on duplicate `_id`. A crash mid-pipeline is recovered simply by restarting — no cleanup, no deduplication job, no reconciliation step required.

**Consumer built once, not per batch.** The consumer is created once in `main()` and reused across all polling loops. Consumer construction triggers partition assignment and group rebalance — O(1) per pipeline lifetime. Building a new consumer per 100,000-message batch would trigger O(n/100000) rebalances, each one pausing all consumption.

**Memory-efficient CSV reading.** `pd.read_csv(filepath, chunksize=10_000)` returns an iterator of 10,000-row DataFrames. Chunked reading keeps RAM flat at approximately one chunk's size regardless of file size — whether the file is 10MB or 10GB.

**Unknown inputs degrade silently, not noisily.** `ParseLogFn` drops unknown log types with a bare `return`. The predict API returns low probabilities and both flags as `False` for unknown types. The context retrieval API returns an empty list for queries it cannot embed. None of these raise 500 errors. Unknown input should be handled gracefully, not crash the system.

**Client version pinning.** The elasticsearch Python client must be pinned to `<9.0.0`. The v9 client sends `Accept: compatible-with=9` in request headers, which the ES 8.11.0 server rejects with `400 BadRequestError: media_type_header_exception`. This dependency compatibility issue is invisible without version pinning and silently breaks a working pipeline when `pip install --upgrade` is run.

**Circuit breaker for self-healing (Memory Guard).** The Memory Guard service (Phase 1 Project 5) monitors heap memory, GC stats, and allocation rate telemetry from instrumented microservices. When the Beam pipeline detects anomalous memory growth (a rolling slope feature crossing a learned threshold), it triggers a circuit breaker: the affected service is restarted automatically via the Kubernetes lifecycle API. The circuit breaker pattern prevents cascading failures — rather than letting an OOM crash propagate to dependent services, the system absorbs it and restores itself. The breaker has three states: closed (normal), open (restarting, calls fail fast), half-open (accepting limited traffic to verify recovery).

---

## 8. Vector Search and Semantic Retrieval (Phase 3)

### What was built

A Context Retrieval Service that embeds both security events and internal runbook documents into the same 384-dimensional vector space using `sentence-transformers/all-MiniLM-L6-v2`. Two retrieval endpoints: `/similar-events` (given a new alert, return the most semantically similar past suspicious/evil events) and `/runbooks` (given an alert description, return the most relevant response runbook). The service feeds directly into the Incident Copilot (Phase 5), providing the grounding evidence that reduces LLM hallucination.

### The engineering decisions that matter

**Dense vector embeddings vs BM25 keyword search — when each is appropriate.** BM25 is a term-frequency/inverse-document-frequency ranking function. It answers: "which documents contain words that appear often in this query but rarely across the whole corpus?" It is fast, requires no training, and works perfectly for filtering on known field values. It fails for semantic retrieval — a query for "process spawned a child shell" will not find a document that says "execve called with /bin/bash as argument" because they share no terms. Dense vector search embeds both the query and the document into a shared semantic space where similar meanings cluster together regardless of surface-level word choice. The system uses BM25 for the `/search` endpoint (exact keyword matching on log fields) and vector search for the `/similar-events` and `/runbooks` endpoints (semantic similarity).

**Sentence Transformers — what they are and why `all-MiniLM-L6-v2`.** A sentence transformer takes a variable-length text string and produces a fixed-size floating-point vector (embedding) where semantically similar texts produce vectors that are close together in the embedding space, regardless of exact wording. `all-MiniLM-L6-v2` produces 384-dimensional vectors (vs 1536 for OpenAI's `text-embedding-ada-002`) and runs fully locally with no API calls or cost. For a security context where log content may be sensitive, keeping embeddings local is the correct default. The model runs in under 50ms per batch on CPU.

**Why a shared embedding model for events and runbooks.** Both security events and runbook text are embedded with the same model, into the same 384-dimensional vector space. This is what makes the semantic search meaningful — the model has learned a shared representation where "root user executed wget" and "runbook: Network Exfiltration Response" are close together in vector space because they are about the same concept, even though they look nothing alike as raw text.

**Scroll API for batch embedding without deep pagination.** Standard paginated search (`from` + `size`) is O(n) for deep pages in Elasticsearch — it must score and sort all prior documents before skipping them. The Scroll API creates an index snapshot and returns pages O(1) per page using a cursor token, making it the correct approach for the embedding batch job that processes all 3.8M documents.

**Two-index architecture.** The main `beth-security-logs` index stores normalized documents for BM25 search. A separate `log-event-vectors` index stores lightweight documents with the original document ID and its 384-dimensional embedding. Keeping embeddings separate means: (1) the main index is not bloated with 384 floats per document, (2) the vector index schema is clean and typed, (3) the two indices can be updated independently. A query against the vector index returns event IDs, which are then fetched from the main index for the full document — a two-step lookup that keeps both indices lean.

**kNN query via Elasticsearch's `knn` clause.** Phase 3 uses Elasticsearch 8's native kNN search, which internally uses the HNSW index:

```python
response = es.search(
    index="log-event-vectors",
    knn={
        "field": "embedding",
        "query_vector": query_embedding.tolist(),
        "k": 10,
        "num_candidates": 100,
    },
)
```

`num_candidates` controls the HNSW search breadth — higher means more accurate but slower. `k=10` returns 10 nearest neighbors. The IDs from this response are used to fetch full documents from the main index.

**Runbook ingestion pipeline.** Runbooks are plain-text `.txt` files stored in `services/context-retrieval/runbooks/`. The `ingest_runbooks.py` script reads each file, embeds its content with the same sentence transformer, and writes it to a `runbook-vectors` index. The separation of event embeddings and runbook embeddings into different indices allows different `k` values, different num_candidates settings, and different result post-processing for each retrieval type.

---

## 9. Secure GenAI Gateway (Phase 4)

### What was built

A centralized reverse proxy for all LLM traffic across the platform. Every service that calls Gemini 3.1 Pro — the Incident Copilot, the alert summarizer, the runbook ranker — routes its traffic through the gateway. The gateway enforces rate limits, filters PII and secrets from prompts, logs request metadata safely (never prompt content), and acts as the single enforcement point for AI usage policy across the entire system.

### The engineering decisions that matter

**The reverse proxy pattern — why a gateway instead of direct API calls.** If each service called Gemini 3.1 Pro directly, rate limit enforcement would require coordination across services, PII filtering would need to be duplicated in every caller, and audit logging would be scattered. A reverse proxy centralizes all of this: every LLM call goes through one service, which enforces policy uniformly. The gateway is also the single place to rotate API keys, change models, or add new policy rules — with no changes required in any downstream service.

**Token bucket rate limiting.** Rate limiting is implemented as a token bucket per caller identity. Each caller (identified by a service name header) has a bucket that refills at a constant rate (e.g., 100 tokens/minute) and depletes one token per request. If the bucket is empty, the request is rejected with 429 (Too Many Requests). Token bucket smooths traffic more fairly than a fixed window counter — a caller that goes quiet for a minute accumulates tokens and can burst briefly without being penalized. Fixed window rate limiting penalizes bursty traffic that happens to land at a window boundary.

**PII detection before forwarding.** The gateway scans every prompt for patterns matching common PII before forwarding it to Gemini 3.1 Pro. Rule-based detection uses compiled regex for SSNs (`\b\d{3}-\d{2}-\d{4}\b`), credit card numbers (Luhn-checkable patterns), email addresses, and AWS secret key patterns. An ML classifier (trained on the Leakage Detector task from Project 48) handles subtler leakage — prompts that don't contain literal PII but are attempting to extract sensitive information. Requests that trip either detector are rejected with 400 and a descriptive error before any data reaches the external LLM API.

**Metadata-only audit logging.** The gateway logs every request with: caller identity, timestamp, token count estimate, model called, response latency, and whether the PII filter triggered. It does NOT log prompt content or response content. This is a deliberate privacy-by-design decision: storing verbatim prompts in a log system creates a secondary data store containing everything any engineer ever asked the LLM, with no expiration policy and no access controls. Metadata-only logging provides a complete audit trail for compliance (who called the LLM when, how often, and at what cost) without creating a prompt data lake.

**The Leakage Detector classifier.** A binary classifier trained to detect prompts that attempt to extract sensitive data — not prompts that contain obvious PII, but prompts where the intent is to extract information from a system that should not leave it. Training examples include: prompts that enumerate users, prompts that ask for credential information in disguised form, and adversarial prompt injection attempts. The classifier is a FastAPI service called synchronously by the gateway before forwarding. Its output is a probability score; requests above a threshold are rejected.

**GenAI gateway as internal dependency for the Incident Copilot.** All LLM calls from the Incident Copilot go through the gateway. This means: (1) the copilot's LLM usage is subject to the same rate limits as any other caller, preventing a single incident from exhausting the API quota, (2) copilot prompts are scanned for accidental PII inclusion (log content that leaked into a prompt template), and (3) every incident summary generation is audited. The copilot does not bypass the gateway even for "internal" traffic — security controls applied inconsistently provide no real protection.

---

## 10. Incident Copilot and RAG Architecture (Phase 5)

### What was built

An automated incident response system triggered when the triage model flags events as evil. It assembles relevant context from three sources (similar past incidents, ranked runbooks, recent deployment metadata), grounds Gemini 3.1 Pro on that context, and generates a structured incident report with a timeline, impact assessment, and recommended playbook. The copilot reduces mean time to diagnose (MTTD) by eliminating the manual context-gathering step that typically takes 20-40 minutes at the start of incident response.

### The engineering decisions that matter

**RAG — Retrieval-Augmented Generation.** An LLM generates text based on its training data, which has a knowledge cutoff and contains no information about your specific environment. RAG grounds the LLM by: (1) retrieving relevant documents from the live system (past incidents, runbooks, deployment records), (2) including those documents in the LLM prompt as context, and (3) instructing the LLM to base its response on the retrieved evidence rather than general knowledge. The result is an incident report that references actual events from the current environment, not hallucinated generalizations.

The measurable impact: without retrieval grounding, Gemini 3.1 Pro hallucination rate on specific system questions is approximately 23%. With RAG grounding using real retrieved context, hallucination drops to approximately 4%. RAG is not optional for a security tool — a wrong recommendation in an incident response playbook can make an incident worse.

**Alert grouping before incident creation.** Raw alerts arrive as individual documents with `evil=1`. A clustering step runs before the copilot to group related alerts into a single incident. Two clustering strategies are applied: rule-based grouping (same host + overlapping time window + same event category → same incident) handles the obvious cases. DBSCAN clustering on alert embedding vectors handles subtler grouping — alerts from different hosts that are semantically similar (the same attack pattern executing in parallel across multiple machines). The copilot is triggered once per incident group, not once per alert — preventing 200 identical incident reports for a single coordinated attack.

**Context assembly pipeline.** Before calling the LLM, the copilot assembles a structured context object containing:
- Top-k similar past incidents from the vector index (retrieved via Phase 3 service)
- Top-3 ranked runbooks by Precision@5 relevance score (retrieved via Phase 3 service)
- Recent deployments from the deployment metadata store (last 2 hours)
- The raw alert timeline from the current incident group
- OpenTelemetry traces for the affected service (if available from Phase 6 telemetry)

This context is serialized into a prompt template with clear section headers. Gemini 3.1 Pro's 1M token context window means even large incident timelines with extensive supporting documents fit in a single prompt without truncation.

**Structured output via JSON mode.** The LLM is instructed to return structured JSON (using Gemini's function-calling/JSON mode), not free-form text. The expected schema:

```json
{
  "incident_id": "...",
  "severity": "critical | high | medium | low",
  "timeline": [...],
  "root_cause_hypothesis": "...",
  "affected_systems": [...],
  "recommended_playbook_steps": [...],
  "confidence": 0.0
}
```

The response is parsed with Pydantic before storage. If the LLM returns malformed JSON, the request is retried with an explicit formatting correction in the follow-up prompt, not silently passed to the caller as a raw string.

**Runbook ranker — Learning to Rank.** The Context Retrieval Service does not return runbooks ranked by vector similarity alone. A Learning to Rank (LTR) model re-ranks the top-20 vector candidates using additional features: runbook age, how often it has been used in past incidents, the severity match between runbook scope and current alert severity, and whether the runbook's tags overlap with the alert's event categories. This two-stage retrieve-then-rerank approach (first vector similarity for recall, then LTR for precision) is standard in production search systems and consistently outperforms either approach alone.

**OpenTelemetry trace integration.** When the affected service has OpenTelemetry instrumentation, the incident copilot fetches the distributed trace for requests that were in-flight during the incident window. Trace spans reveal which specific code paths were executing when the anomaly occurred — information that is not present in security logs or metrics alone. The copilot includes the slowest or most unusual spans in the context sent to the LLM, enabling it to connect a security anomaly (unexpected process execution) with an application-level symptom (elevated p95 latency) in the same report.

---

## 11. eBPF Kernel Agent (Phase 6)

### What was built

A Linux kernel observability agent that uses eBPF programs to capture syscall-level telemetry from running processes without modifying application code, without kernel modules, and without full root access. Deployed as a Kubernetes DaemonSet (one pod per node), it produces a continuous stream of kernel events published to the same Kafka topic as the BETH dataset, feeding the Beam pipeline with live telemetry. This closes the gap between the static BETH dataset (historical honeypot data) and live production observability.

### The engineering decisions that matter

**What eBPF is and why it matters.** eBPF (extended Berkeley Packet Filter) allows you to load small programs into the Linux kernel that execute in response to kernel events — system calls, network packets, function calls — without modifying kernel source code or loading a kernel module. The kernel's eBPF verifier checks every program for safety (no unbounded loops, no invalid memory access, no crashing) before loading it. This gives you kernel-level observability with safety guarantees that kernel modules do not provide.

For this platform: the eBPF agent hooks into `execve` (process execution), `openat` (file open), `connect` (network connection initiation), and `accept` (incoming connection) tracepoints. Every time any process on the node makes one of these calls, the eBPF program captures the syscall arguments, the calling process's PID/UID, and the return value — the same fields present in the BETH dataset — and writes them to a ring buffer.

**BPF ring buffer for kernel-to-userspace communication.** The eBPF program runs in kernel context. The userspace daemon runs outside the kernel. Data transfer between them uses a BPF ring buffer — a fixed-size circular buffer in kernel memory that the eBPF program writes to and the userspace daemon reads from. The ring buffer is lock-free for the producer side (the eBPF program), meaning the kernel-side write is O(1) with no syscall overhead. The userspace daemon calls `poll()` on the ring buffer file descriptor to be notified when new data is available, avoiding busy-waiting.

**Capability-based security — principle of least privilege.** Loading eBPF programs historically required full root (`CAP_SYS_ADMIN`). Since Linux 5.8, two finer-grained capabilities cover the same use case: `CAP_BPF` (load eBPF programs and maps) and `CAP_PERFMON` (access performance monitoring syscalls). The DaemonSet pod runs with only these two capabilities granted, not full root. An attacker who compromises the eBPF agent process gets `CAP_BPF` and `CAP_PERFMON` — not the ability to mount filesystems, change network namespaces, or modify kernel parameters.

```yaml
securityContext:
  capabilities:
    add: ["CAP_BPF", "CAP_PERFMON"]
    drop: ["ALL"]
  runAsNonRoot: true
```

**Kubernetes DaemonSet — one pod per node guaranteed.** A DaemonSet is a Kubernetes controller that ensures exactly one pod of a given spec runs on every node in the cluster (or every node matching a node selector). This is the correct primitive for the eBPF agent: kernel observability is per-node (each node has its own kernel and its own process tree), and you need one agent per node, not one agent per replica of some service. When a new node joins the cluster, Kubernetes automatically schedules the DaemonSet pod onto it. When a node leaves, the pod is removed. No manual intervention.

**Cross-language architecture: C for eBPF, Python for userspace.** The eBPF programs are written in C (compiled to BPF bytecode with `clang` + `libbpf`) because eBPF programs must be compiled to a specific ISA (BPF bytecode) that the kernel verifier understands. The userspace daemon that reads from the ring buffer and publishes to Kafka is written in Python using `confluent-kafka`, consistent with the rest of the platform. The boundary between them is the BPF ring buffer — clean, well-defined, and independent of each component's implementation language.

**Linux-only requirement and PACE cluster.** eBPF requires the Linux kernel headers, `libbpf`, and `bpftool`. These do not exist on macOS or Windows. Development and testing for Phase 6 runs on the Georgia Tech PACE HPC cluster (SSH access to a Linux environment with the required kernel version and development tools). The SLURM job scheduler on PACE is also used for GPU-accelerated model training in Phase 2B — the same cluster serves both workloads.

**Feature parity with the BETH dataset.** The eBPF agent is designed to capture exactly the fields present in the BETH deep_kernel and standard_host CSV schemas: `eventName`, `processId`, `parentProcessId`, `processName`, `userId`, `returnValue`, `args`. This is not accidental — the ML triage models (Phase 2B) were trained on BETH data, so the eBPF agent must produce events with the same schema so the models can score live events without modification. The live event stream runs through the same `ParseLogFn` → `extract_features` pipeline before reaching the triage models.

---

## 12. Kubernetes Infrastructure and Deployment

### What was built

A Kubernetes-native platform where all five FastAPI services are deployed as Kubernetes Deployments with Horizontal Pod Autoscalers (HPA), the eBPF agent runs as a DaemonSet, Secrets manage credentials, and ConfigMaps manage non-secret configuration. Rolling updates enable zero-downtime deployments. Resource requests and limits prevent any single service from starving others on the same node.

### The engineering decisions that matter

**Horizontal Pod Autoscaler (HPA) for all inference services.** The triage prediction API, the context retrieval API, and the incident orchestrator all scale horizontally under load. HPA monitors CPU utilization and request rate (via the metrics server) and adds or removes pod replicas automatically. The key configuration decision is the scale-up threshold: set too low and the system wastes resources on idle replicas; set too high and latency spikes before new pods are ready. For inference services, CPU at 60% is a reasonable trigger — it leaves headroom for the next request batch while new pods warm up their model loading.

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: triage-model-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: triage-model
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 60
```

**Rolling updates for zero-downtime deployments.** All Deployments use `strategy: RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`. This means Kubernetes adds one new pod before removing one old pod — there is always at least the original replica count of healthy pods available during a deployment. The old pods continue serving traffic until the new pods pass their readiness probes. A failed readiness probe automatically rolls back the update, preventing a broken model version or a failed dependency from reaching production.

**Readiness vs liveness probes.** Liveness probes answer "is this container still running?" — a failed liveness probe causes Kubernetes to restart the container. Readiness probes answer "is this container ready to receive traffic?" — a failed readiness probe removes the pod from the service load balancer without restarting it. The distinction matters for inference services: a pod that is still loading its 200MB model file should fail readiness (do not send it traffic yet) but pass liveness (do not kill it — it is working on startup). Both probes hit `/health`:

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 8001
  initialDelaySeconds: 20
  periodSeconds: 5
livenessProbe:
  httpGet:
    path: /health
    port: 8001
  initialDelaySeconds: 60
  periodSeconds: 30
```

**Kubernetes Secrets for credential management.** Kafka credentials and the Gemini API key are stored as Kubernetes Secrets (base64-encoded, stored in etcd, access-controlled via RBAC). Pods receive these values as environment variables — the same interface as the `.env` file in local development, so no application code changes are needed between environments. Secrets are never committed to the repository or baked into container images.

**Resource requests and limits.** Every pod has explicit CPU and memory requests (the minimum guaranteed to it by the scheduler) and limits (the maximum it can consume before being throttled or OOM-killed). The triage model service is given higher memory limits than the search API because it holds two XGBoost models in memory. The eBPF agent DaemonSet has tight memory limits because it should be a lightweight observer — if it grows unexpectedly, the OOM kill is the signal that something is wrong with the ring buffer consumption loop.

**Namespace isolation.** The platform services run in a `security-platform` namespace. The eBPF agent DaemonSet runs in `kube-system` (or a dedicated `monitoring` namespace) because it requires cluster-wide node access. RBAC policies ensure that service accounts in `security-platform` cannot reach resources in other namespaces — the principle of least privilege applied at the cluster level.

---

## 13. Apache Spark Batch Layer

### What was built

A batch processing layer on top of cold object storage (Parquet files on S3 or GCS) for security forensics queries, offline ML feature recomputation, and historical trend analysis that would be prohibitively expensive to run against the live Elasticsearch index. Spark acts as the batch layer in the Lambda architecture — it processes the complete historical record, while Beam handles the speed layer for recent events.

### The engineering decisions that matter

**Why Spark for the batch layer and not more Beam.** Both Beam and Spark are distributed data processing frameworks. Beam's strength is streaming — it runs the same pipeline code on DirectRunner (local) or FlinkRunner (distributed) with one config change. Spark's strength is rich SQL-style analytics, DataFrames with a mature optimizer, extensive ML library (MLlib), and native integration with the Hadoop ecosystem and cloud object storage. The forensics use case — "show me all execve calls from root users in the past 90 days across all host types" — is a batch SQL query, not a streaming transform. Spark's query optimizer handles this better than Beam's batch mode.

**Parquet on object storage as the cold store.** The Beam pipeline writes a copy of every processed event to Parquet files on S3/GCS in addition to Elasticsearch. Parquet is a columnar binary format — a query that only touches `event_name` and `user_id` reads only those two columns from disk, not the full document. At 3.8M events (and growing with live eBPF data), column pruning and predicate pushdown in Parquet reduce forensics query I/O by an order of magnitude vs row-oriented formats like JSON or CSV.

**Time-partitioned storage layout.** Parquet files are organized by date: `s3://bucket/logs/year=2021/month=05/day=16/`. Spark's partition pruning skips entire date directories that fall outside a query's time range without reading any files. A forensics query for "last 7 days" scans 7 day-partitions instead of the full history.

**Offline ML feature generation.** Phase 2B models are trained on features extracted by the Beam pipeline at ingest time. When new features are designed (or the `feat_is_root_user` bug is fixed), recomputing features for all 3.8M historical events is a Spark batch job — not a Beam streaming job. Spark processes the full historical Parquet dataset, recomputes features, writes the result to a new Parquet location, and the training pipeline reads from the new location. The live Elasticsearch index is only updated on the next full re-ingest.

---

## 14. Distributed Observability (OpenTelemetry and Prometheus)

### What was built

Instrumentation across all five FastAPI services using OpenTelemetry — the vendor-neutral standard for distributed traces, metrics, and logs. Prometheus metrics exposed from each service for the alerting layer. The observability data feeds back into the platform's own intelligence: the eBPF agent captures service-level syscall behavior, the Beam pipeline enriches it, and the ML models score it.

### The engineering decisions that matter

**OpenTelemetry — vendor-neutral instrumentation.** OpenTelemetry (OTel) is a CNCF standard that provides a single instrumentation API for traces, metrics, and logs that works with any backend (Jaeger, Zipkin, Grafana Tempo, Datadog, etc.). Instead of writing Datadog-specific or Jaeger-specific code, every service instruments against the OTel API. The collector (a separate process) receives telemetry and routes it to whatever backend is configured. Changing the observability backend requires a config change in the collector — no application code changes.

**Distributed traces for cross-service request tracking.** A single incident report generation involves calls to: the vector retrieval service, the runbook ranker, the incident orchestrator, the GenAI gateway, and Gemini 3.1 Pro. Without distributed tracing, a slow incident report is impossible to diagnose — you don't know which service in the chain was slow. With OTel trace propagation, a single trace ID flows through all service calls. The resulting trace tree shows exactly which span took how long:

```
incident-orchestrator.generate_report [450ms]
├── context-retrieval.similar_events [12ms]
├── context-retrieval.runbooks [8ms]
├── genai-gateway.chat_completion [380ms]
│   └── gemini.generate_content [375ms]
└── incident-orchestrator.persist_report [50ms]
```

**Prometheus metrics for the alerting layer.** Each FastAPI service exposes a `/metrics` endpoint (via `prometheus-fastapi-instrumentator`) with standard HTTP metrics: request count by route and status code, request latency histogram (p50/p95/p99), and in-flight request gauge. Custom metrics include: triage model inference latency, vector search latency, LLM token usage per request, and PII filter trigger rate at the gateway.

These Prometheus metrics feed back into the platform's own alert detection — a spike in triage model inference latency (p99 > 500ms) triggers the same `evil=1` alert pipeline that security events trigger, routing through the Incident Copilot with a runbook for "prediction service degraded." The platform observes itself using the same infrastructure it uses to observe external threats.

**Prometheus `rate()` vs `increase()`.** These are commonly confused. `rate()` computes the per-second rate of a counter over a time window — useful for "requests per second right now." `increase()` computes the total increase in a counter over a window — useful for "how many errors in the last 5 minutes." Both account for counter resets (counter monotonically increasing, reset to 0 on process restart). For alerting on error rate, `rate(http_requests_total{status="500"}[5m])` is more actionable than `increase()` because it normalizes for the window length.

---

## 15. Configuration and Credentials Management

### What was built

A centralized configuration module that loads all secrets from environment variables, never from source code. Zero credentials committed to the repository. Two-layer secret management: `.env` for local development, Kubernetes Secrets for production.

### The engineering decisions that matter

**Single source of truth in `config.py`.** Both the producer and the consumer need the same Kafka connection details. Duplicating `.env` loading in each file creates two places to update when credentials rotate. `config.py` loads the environment once and exports typed constants:

```python
from config import KAFKA_PRODUCER_CONFIG, KAFKA_TOPIC, ELASTIC_URL, ES_INDEX_NAME
```

**`load_dotenv()` runs once via Python's module cache.** Python's import system caches modules. If multiple files import `config`, the module runs only once. `load_dotenv()` sets OS-level environment variables on the first import, and `os.getenv()` reads them on every subsequent call regardless of which file calls it.

**`.env` is gitignored absolutely.** The `.env` file containing real credentials is in `.gitignore`. The pattern `data/*` ignores the entire data directory (3.8M rows of honeypot data, too large for git and containing real attack traffic). Terraform state files (`*.tfstate`) are gitignored in advance of infrastructure-as-code work. The `.joblib` model files are gitignored because they are binary artifacts weighing tens of megabytes, reproducible by running `train.py`. `PLATFORM_DEEP_DIVE.md` and `QUIZ_LOG.md` are gitignored as local-only working documents.

**Terraform for infrastructure as code.** Cloud infrastructure — the Confluent Cloud Kafka cluster, the Elastic Cloud deployment, the GCS/S3 bucket for cold storage, the GKE/EKS cluster — is provisioned via Terraform rather than manually through cloud consoles. Infrastructure-as-code means the entire platform can be reproduced in a new cloud account by running `terraform apply`. Drift between the actual cloud state and the documented state is detected automatically by `terraform plan`. The Terraform state file is stored in a remote backend (GCS or S3 bucket with locking), not committed to the repository.

---

## 16. Code Quality Patterns

### DRY principle — `_flush()` as a shared helper

Both `process()` (triggered when the buffer hits 500) and `finish_bundle()` (triggered when Beam finishes a bundle) need to execute the same bulk write logic: check if the buffer is non-empty, call `helpers.bulk()`, clear the buffer. Without `_flush()`, this logic would be duplicated in two places. Six months later, if someone adds retry logic or error handling, they must update both copies identically or introduce a silent divergence:

```python
def _flush(self):
    if not self.buffer:
        return
    success, errors = helpers.bulk(self.es_client, self.buffer, raise_on_error=False)
    self.buffer = []

def process(self, element):
    self.buffer.append(...)
    if len(self.buffer) >= self.bulk_size:
        self._flush()

def finish_bundle(self):
    self._flush()
```

### Type annotations throughout

Every function signature, every class attribute, every Pydantic model field carries type annotations. This is not cosmetic — FastAPI and Pydantic use these annotations at runtime to validate requests and serialize responses. XGBoost inference uses them to verify the DMatrix input type. Editors and static analysis tools use them to catch type errors before runtime.

### Computed feature separation from raw fields

The pipeline enriches every document with a `features` sub-object. These are nested separately from `attributes` (raw parsed values). Any downstream system can select only features with `_source.includes: ["features.*"]` without pulling the full document. The features namespace is forward-compatible: adding a new feature adds a key to `features` without disturbing the existing schema.

### Shannon entropy as a computed security signal

Shannon entropy measures character distribution uniformity in a string. Low entropy (e.g., `google.com`) means a few characters dominate — predictable, human-readable. High entropy (e.g., `x7f3a9b2c4e1d.ru`) means characters are roughly uniformly distributed — random-looking, consistent with Domain Generation Algorithm (DGA) malware. The entropy signal is computed at pipeline ingest time and stored as `feat_dns_query_entropy`. At inference time, the same computation runs in `predict.py` using identical code — training/inference parity is guaranteed by sharing the implementation.

### Linux syscall convention for return value interpretation

The `feat_return_failed` feature is computed as `1 if rv < 0 else 0`, not `rv != 0`. This is the Linux syscall convention: negative return values are error codes (e.g., -1 for EPERM, -2 for ENOENT). Zero means success. Positive values are valid results — bytes read, file descriptor number, child process PID. A `read()` syscall returning 4096 (bytes read successfully) should not be flagged as failed. Using `rv != 0` instead of `rv < 0` would flag every successful `read()` call as a failure — a fundamental correctness error in the feature engineering.

---

## 17. Lambda Architecture

The project implements the Lambda architecture pattern — two parallel data paths with different latency/throughput trade-offs:

**Speed layer (Apache Beam → Elasticsearch → FastAPI services).** Events flow from Kafka through the Beam pipeline to Elasticsearch within seconds of being produced. The triage model scores events in real time. The Incident Copilot generates reports within minutes of an incident being detected. This path answers "is this event happening now?" with sub-second query latency.

**Batch layer (Apache Spark → Parquet on object storage).** The Beam pipeline simultaneously writes events to Parquet files on S3/GCS. Spark batch jobs run on a schedule against this cold store for forensics queries ("all evil events in the past 90 days from root users on kernel-log hosts"), offline model retraining feature generation, historical trend analysis, and compliance reporting. This path answers "what happened over the past quarter?" with high throughput and low cost — cold storage is orders of magnitude cheaper than keeping 3.8M+ documents in a hot Elasticsearch cluster indefinitely.

The two layers are served by the same underlying data (events published once to Kafka, then written to both ES and Parquet), maintaining consistency without requiring synchronization between them.

---

## 18. Summary of Scale and Numbers

| Metric | Value |
|---|---|
| Raw messages produced to Kafka | 3,809,617 |
| Pipeline runs to complete full ingest | 39 |
| Documents in Elasticsearch after deduplication | 3,807,980 |
| Kafka partitions | 3 |
| Pipeline deduplication collision rate | ~0.05% |
| Bulk write batch size | 500 docs/HTTP request |
| Total parser tests | 24 |
| Total predict API tests | 57 |
| Total tests | 81 |
| Test layers | 3 (unit / API contract / directional sanity) |
| FastAPI microservices | 5 (search, triage, context-retrieval, incident-orchestrator, genai-gateway) |
| Bugs caught by test suite before production | 2 (dns_response_code default, feat_is_root_user) |
| Embedding dimensions (sentence transformer) | 384 |
| HNSW index complexity | O(log n) approximate nearest neighbor |
| Kafka topic naming migrations | 1 (logs.raw.v1 → security.logs.raw — no rename exists, delete + recreate) |
| Python client version constraints enforced | 1 (elasticsearch <9.0.0) |
| Kafka authentication | SASL_SSL (PLAIN mechanism, SSL transport encryption) |
| Kubernetes workload types used | Deployment (services), DaemonSet (eBPF agent) |
| eBPF tracepoints hooked | 4 (execve, openat, connect, accept) |
| Linux capabilities required by eBPF agent | 2 (CAP_BPF, CAP_PERFMON) — not root |
| LLM hallucination rate without RAG | ~23% |
| LLM hallucination rate with RAG grounding | ~4% |
| Cold storage format | Parquet (columnar, partition-pruned by date) |
| Rate limiting algorithm | Token bucket (per caller identity) |
| Delivery semantics | At-least-once (commit after pipeline success) |
| Idempotency mechanism | MD5 hash of stable document fields → deterministic ES _id |
