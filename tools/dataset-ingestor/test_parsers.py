"""
Unit tests for the parser classes in parsers.py.

HOW TO RUN:
    pytest tools/dataset-ingestor/test_parsers.py -v

WHY THESE TESTS EXIST:
    Parsers are the first transformation in the pipeline. If they silently drop
    a field, produce the wrong type, or mishandle missing data, every downstream
    component (Elasticsearch, ML model, search API) gets corrupt data.
    These tests catch parser bugs at the source before they propagate.

STRUCTURE: Arrange -> Act -> Assert
    Each test sets up a known input, calls the parser, and checks the output.
    Tests are intentionally deterministic — same input always gives same output.
"""

import pytest
from parsers import DnsParser, DeepKernelParser, StandardHostParser


# ---------------------------------------------------------------------------
# Fixtures — reusable input data shared across tests
# ---------------------------------------------------------------------------

VALID_DNS_ROW = {
    "Timestamp": "2021-05-16T17:13:14Z",
    "SourceIP": "10.100.1.95",
    "DestinationIP": "10.100.0.2",
    "DnsQuery": "ssm.us-east-2.amazonaws.com",
    "DnsResponseCode": 0,
    "sus": 0,
    "evil": 0,
}

VALID_DNS_ROW_ATTACK = {
    "Timestamp": "2021-05-16T19:00:00Z",
    "SourceIP": "10.100.1.1",
    "DestinationIP": "8.8.8.8",
    "DnsQuery": "malicious-c2-server.ru",
    "DnsResponseCode": 3,
    "sus": 1,
    "evil": 1,
}

VALID_KERNEL_ROW = {
    "timestamp": 1621184000.0,
    "eventName": "execve",
    "hostName": "ip-10-100-1-105",
    "eventId": 59,
    "returnValue": 0,
    "processId": 1234,
    "parentProcessId": 1,
    "processName": "bash",
    "userId": 1000,
    "threadId": 1234,
    "mountNamespace": 4026531840,
    "stackAddresses": "[]",
    "sus": 0,
    "evil": 0,
}

VALID_HOST_ROW = {
    "timestamp": 1621184000.0,
    "eventName": "openat",
    "hostName": "ip-10-100-1-26",
    "eventId": 257,
    "returnValue": 3,
    "processId": 5678,
    "parentProcessId": 1000,
    "processName": "python3",
    "userId": 1000,
    "sus": 1,
    "evil": 0,
}


# ---------------------------------------------------------------------------
# DnsParser tests
# ---------------------------------------------------------------------------

class TestDnsParser:

    def setup_method(self):
        self.parser = DnsParser()

    def test_output_has_required_top_level_keys(self):
        result = self.parser.parse(VALID_DNS_ROW)
        assert "timestamp" in result
        assert "log_attribute" in result
        assert "attributes" in result
        assert "labels" in result

    def test_log_attribute_is_correct_value(self):
        result = self.parser.parse(VALID_DNS_ROW)
        assert result["log_attribute"] == "network_dns"

    def test_timestamp_is_mapped_correctly(self):
        result = self.parser.parse(VALID_DNS_ROW)
        assert result["timestamp"] == "2021-05-16T17:13:14Z"

    def test_attributes_contain_correct_values(self):
        result = self.parser.parse(VALID_DNS_ROW)
        attrs = result["attributes"]
        assert attrs["source_ip"] == "10.100.1.95"
        assert attrs["destination_ip"] == "10.100.0.2"
        assert attrs["dns_query"] == "ssm.us-east-2.amazonaws.com"
        assert attrs["dns_response_code"] == 0

    def test_labels_benign(self):
        result = self.parser.parse(VALID_DNS_ROW)
        assert result["labels"]["sus"] == 0
        assert result["labels"]["evil"] == 0

    def test_labels_attack(self):
        result = self.parser.parse(VALID_DNS_ROW_ATTACK)
        assert result["labels"]["sus"] == 1
        assert result["labels"]["evil"] == 1

    def test_dns_response_code_type_is_not_string(self):
        result = self.parser.parse(VALID_DNS_ROW)
        assert not isinstance(result["attributes"]["dns_response_code"], str), (
            "dns_response_code should not be a string. "
            "Check the default value in DnsParser."
        )

    def test_missing_dns_response_code_defaults_to_none(self):
        row_without_response_code = {k: v for k, v in VALID_DNS_ROW.items()
                                      if k != "DnsResponseCode"}
        result = self.parser.parse(row_without_response_code)
        assert result["attributes"]["dns_response_code"] is None, (
            f"Expected None for missing DnsResponseCode, "
            f"got {repr(result['attributes']['dns_response_code'])}"
        )

    def test_missing_labels_default_to_zero(self):
        row = {k: v for k, v in VALID_DNS_ROW.items() if k not in ("sus", "evil")}
        result = self.parser.parse(row)
        assert result["labels"]["sus"] == 0
        assert result["labels"]["evil"] == 0

    def test_missing_timestamp_defaults_to_empty_string(self):
        row = {k: v for k, v in VALID_DNS_ROW.items() if k != "Timestamp"}
        result = self.parser.parse(row)
        assert result["timestamp"] == ""

    def test_empty_row_does_not_raise(self):
        result = self.parser.parse({})
        assert result["log_attribute"] == "network_dns"
        assert result["attributes"]["dns_response_code"] is None


# ---------------------------------------------------------------------------
# DeepKernelParser tests
# ---------------------------------------------------------------------------

class TestDeepKernelParser:

    def setup_method(self):
        self.parser = DeepKernelParser()

    def test_output_has_required_top_level_keys(self):
        result = self.parser.parse(VALID_KERNEL_ROW)
        assert "timestamp" in result
        assert "event_name" in result
        assert "attributes" in result
        assert "labels" in result

    def test_event_name_is_mapped_correctly(self):
        result = self.parser.parse(VALID_KERNEL_ROW)
        assert result["event_name"] == "execve"

    def test_timestamp_is_string(self):
        result = self.parser.parse(VALID_KERNEL_ROW)
        assert isinstance(result["timestamp"], str)

    def test_attributes_contain_process_info(self):
        result = self.parser.parse(VALID_KERNEL_ROW)
        attrs = result["attributes"]
        assert attrs["host_name"] == "ip-10-100-1-105"
        assert attrs["process_name"] == "bash"
        assert attrs["process_id"] == 1234

    def test_missing_event_name_defaults_to_unknown(self):
        row = {k: v for k, v in VALID_KERNEL_ROW.items() if k != "eventName"}
        result = self.parser.parse(row)
        assert result["event_name"] == "unknown_kernel_event"

    def test_labels_attack(self):
        row = {**VALID_KERNEL_ROW, "sus": 1, "evil": 1}
        result = self.parser.parse(row)
        assert result["labels"]["sus"] == 1
        assert result["labels"]["evil"] == 1

    def test_empty_row_does_not_raise(self):
        result = self.parser.parse({})
        assert result["event_name"] == "unknown_kernel_event"


# ---------------------------------------------------------------------------
# StandardHostParser tests
# ---------------------------------------------------------------------------

class TestStandardHostParser:

    def setup_method(self):
        self.parser = StandardHostParser()

    def test_output_has_required_top_level_keys(self):
        result = self.parser.parse(VALID_HOST_ROW)
        assert "timestamp" in result
        assert "event_name" in result
        assert "attributes" in result
        assert "labels" in result

    def test_event_name_is_mapped_correctly(self):
        result = self.parser.parse(VALID_HOST_ROW)
        assert result["event_name"] == "openat"

    def test_missing_event_name_defaults_to_unknown(self):
        row = {k: v for k, v in VALID_HOST_ROW.items() if k != "eventName"}
        result = self.parser.parse(row)
        assert result["event_name"] == "unknown_event"

    def test_attributes_do_not_contain_kernel_only_fields(self):
        result = self.parser.parse(VALID_HOST_ROW)
        attrs = result["attributes"]
        assert "thread_id" not in attrs
        assert "mount_namespace" not in attrs
        assert "stack_addresses" not in attrs

    def test_labels_sus_only(self):
        result = self.parser.parse(VALID_HOST_ROW)
        assert result["labels"]["sus"] == 1
        assert result["labels"]["evil"] == 0

    def test_empty_row_does_not_raise(self):
        result = self.parser.parse({})
        assert result["event_name"] == "unknown_event"
