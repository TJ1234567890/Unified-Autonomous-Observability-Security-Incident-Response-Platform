"""
Integration + unit tests for the Security Log Search API.

REQUIRES: Elasticsearch running at localhost:9200 with the beth-security-logs
index populated. Run `docker-compose up -d` and the ingestor before running
these tests.

HOW TO RUN:
    pytest services/search-api/test_search_api.py -v

WHY TestClient AND NOT subprocess:
    FastAPI's TestClient runs the ASGI app in-process -- no uvicorn subprocess,
    no port binding, no sleep-and-wait startup. It drives the full request/response
    cycle including Pydantic validation and route handlers, just without a real
    HTTP server. This is the standard FastAPI testing pattern.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

# Add this directory to sys.path so 'from models import ...' works when pytest
# runs from the project root (which is not inside services/search-api/).
sys.path.insert(0, os.path.dirname(__file__))

from main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200_when_es_is_up(self):
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "es_url" in body
        assert "index" in body


# ---------------------------------------------------------------------------
# log_type filter
# ---------------------------------------------------------------------------

class TestLogTypeFilter:
    def test_dns_returns_hits(self):
        response = client.post("/search", json={"log_type": "dns", "page_size": 5})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for hit in data["hits"]:
            assert hit["source"]["log_type"] == "dns"

    def test_deep_kernel_returns_hits(self):
        response = client.post("/search", json={"log_type": "deep_kernel", "page_size": 5})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for hit in data["hits"]:
            assert hit["source"]["log_type"] == "deep_kernel"

    def test_standard_host_returns_hits(self):
        response = client.post("/search", json={"log_type": "standard_host", "page_size": 5})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for hit in data["hits"]:
            assert hit["source"]["log_type"] == "standard_host"

    def test_three_types_sum_to_total(self):
        dns   = client.post("/search", json={"log_type": "dns",           "page_size": 1}).json()["total"]
        dk    = client.post("/search", json={"log_type": "deep_kernel",   "page_size": 1}).json()["total"]
        sh    = client.post("/search", json={"log_type": "standard_host", "page_size": 1}).json()["total"]
        total = client.post("/search", json={"page_size": 1}).json()["total"]
        assert dns + dk + sh == total

    def test_invalid_log_type_returns_422(self):
        response = client.post("/search", json={"log_type": "kernel"})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("log_type" in str(e) for e in detail)

    def test_empty_string_log_type_returns_422(self):
        response = client.post("/search", json={"log_type": ""})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# sus / evil label filters
# ---------------------------------------------------------------------------

class TestLabelFilters:
    def test_evil_1_returns_only_evil_docs(self):
        response = client.post("/search", json={"evil": 1, "page_size": 10})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for hit in data["hits"]:
            assert hit["source"]["labels"]["evil"] == 1

    def test_evil_0_excludes_evil_docs(self):
        response = client.post("/search", json={"evil": 0, "page_size": 10})
        assert response.status_code == 200
        for hit in response.json()["hits"]:
            assert hit["source"]["labels"]["evil"] == 0

    def test_sus_1_returns_results(self):
        response = client.post("/search", json={"sus": 1, "page_size": 5})
        assert response.status_code == 200
        assert response.json()["total"] > 0

    def test_combined_log_type_and_sus(self):
        response = client.post("/search", json={"log_type": "deep_kernel", "sus": 1, "page_size": 5})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for hit in data["hits"]:
            assert hit["source"]["log_type"] == "deep_kernel"
            assert hit["source"]["labels"]["sus"] == 1

    def test_invalid_sus_value_returns_422(self):
        response = client.post("/search", json={"sus": 2})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Full-text dns_query (must clause -- BM25 scoring)
# ---------------------------------------------------------------------------

class TestFullTextSearch:
    def test_dns_query_returns_scored_hits(self):
        response = client.post("/search", json={"dns_query": "amazonaws.com", "page_size": 5})
        assert response.status_code == 200
        data = response.json()
        assert data["total"] > 0
        for hit in data["hits"]:
            # must clause produces non-zero BM25 scores, unlike filter
            assert hit["score"] is not None
            assert hit["score"] > 0

    def test_filter_only_produces_zero_score(self):
        response = client.post("/search", json={"log_type": "dns", "page_size": 5})
        assert response.status_code == 200
        for hit in response.json()["hits"]:
            # filter clause does no scoring -- score is 0.0
            assert hit["score"] == 0.0


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_page_2_differs_from_page_1(self):
        r1 = client.post("/search", json={"log_type": "dns", "page": 1, "page_size": 5})
        r2 = client.post("/search", json={"log_type": "dns", "page": 2, "page_size": 5})
        assert r1.status_code == 200
        assert r2.status_code == 200
        ids_p1 = {h["id"] for h in r1.json()["hits"]}
        ids_p2 = {h["id"] for h in r2.json()["hits"]}
        assert ids_p1.isdisjoint(ids_p2), "Page 1 and Page 2 must not share document IDs"

    def test_page_size_zero_returns_422(self):
        response = client.post("/search", json={"page_size": 0})
        assert response.status_code == 422

    def test_page_size_over_500_returns_422(self):
        response = client.post("/search", json={"page_size": 501})
        assert response.status_code == 422

    def test_page_zero_returns_422(self):
        response = client.post("/search", json={"page": 0})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Time range filter
# ---------------------------------------------------------------------------

class TestTimeRange:
    def test_time_range_returns_results(self):
        response = client.post("/search", json={
            "log_type": "dns",
            "timestamp_from": "2021-05-16T17:00:00Z",
            "timestamp_to":   "2021-05-16T18:00:00Z",
            "page_size": 5,
        })
        assert response.status_code == 200
        assert response.json()["total"] > 0

    def test_impossible_time_range_returns_zero(self):
        response = client.post("/search", json={
            "timestamp_from": "2099-01-01T00:00:00Z",
            "timestamp_to":   "2099-01-02T00:00:00Z",
        })
        assert response.status_code == 200
        assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# Query builder -- pure unit tests, no ES required
# ---------------------------------------------------------------------------

class TestQueryBuilder:
    """Tests for build_query() logic only. No Elasticsearch involved."""

    def setup_method(self):
        from search import build_query
        from models import SearchRequest
        self.build_query = build_query
        self.SearchRequest = SearchRequest

    def test_empty_request_produces_match_all(self):
        q = self.build_query(self.SearchRequest())
        assert q == {"match_all": {}}

    def test_log_type_goes_in_filter_not_must(self):
        q = self.build_query(self.SearchRequest(log_type="dns"))
        assert "bool" in q
        assert "filter" in q["bool"]
        assert {"term": {"log_type": "dns"}} in q["bool"]["filter"]
        assert "must" not in q["bool"]

    def test_dns_query_goes_in_must_not_filter(self):
        q = self.build_query(self.SearchRequest(dns_query="amazonaws.com"))
        assert "bool" in q
        assert "must" in q["bool"]
        assert {"match": {"attributes.dns_query": "amazonaws.com"}} in q["bool"]["must"]
        assert "filter" not in q["bool"]

    def test_combined_filter_and_must_both_present(self):
        q = self.build_query(self.SearchRequest(log_type="dns", dns_query="amazonaws.com"))
        assert "filter" in q["bool"]
        assert "must" in q["bool"]

    def test_sus_goes_in_filter(self):
        q = self.build_query(self.SearchRequest(sus=1))
        assert {"term": {"labels.sus": 1}} in q["bool"]["filter"]

    def test_evil_goes_in_filter(self):
        q = self.build_query(self.SearchRequest(evil=1))
        assert {"term": {"labels.evil": 1}} in q["bool"]["filter"]

    def test_time_range_goes_in_filter(self):
        q = self.build_query(self.SearchRequest(
            timestamp_from="2021-05-16T00:00:00Z",
            timestamp_to="2021-05-17T00:00:00Z",
        ))
        range_clause = {"range": {"timestamp": {
            "gte": "2021-05-16T00:00:00Z",
            "lte": "2021-05-17T00:00:00Z",
        }}}
        assert range_clause in q["bool"]["filter"]
