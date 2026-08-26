"""
PII and secret detector for the Secure GenAI Gateway.

Scans prompt text before it reaches the LLM. If a match is found, the request
is rejected (422) and logged (metadata only -- the prompt is never stored).

WHY REGEX AND NOT AN ML CLASSIFIER:
    An ML classifier trained on known-leakage examples would catch more subtle
    cases (encoded secrets, obfuscated PII). But regex is deterministic, has zero
    false negatives for the patterns it covers, adds no latency, and requires no
    model management. The CLAUDE.md roadmap calls for the ML classifier
    (Leakage Detector) in Phase 4's advanced iteration. This regex layer acts as
    the always-on baseline -- cheap, reliable, zero-latency.

LIMITATION: Regex cannot catch base64-encoded or otherwise obfuscated secrets.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PiiMatch:
    pii_type: str
    start: int
    end: int
    # Only first 20 chars of the match are retained. Used for audit logging only.
    # This class is NEVER serialized to an API response -- the gateway returns
    # only the pii_type list, never the snippet.
    snippet: str


_PATTERNS: dict[str, re.Pattern] = {
    # US Social Security Number
    "ssn":              re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    # Major card prefixes: Visa, MC, Amex, Discover
    "credit_card":      re.compile(
        r'\b(?:4[0-9]{12}(?:[0-9]{3})?'
        r'|5[1-5][0-9]{14}'
        r'|3[47][0-9]{13}'
        r'|6(?:011|5[0-9]{2})[0-9]{12})\b'
    ),
    "email":            re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    "phone_us":         re.compile(r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b'),
    # AWS access key IDs start with AKIA (long-term) or ASIA (temp/STS)
    "aws_access_key":   re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    # PEM private key header
    "private_key_pem":  re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    # OpenAI API key prefix
    "openai_api_key":   re.compile(r'\bsk-[A-Za-z0-9]{20,}\b'),
    # Generic credential assignment patterns (case-insensitive)
    "password_field":   re.compile(r'(?i)\bpassword\s*[:=]\s*\S{4,}'),
    "secret_field":     re.compile(r'(?i)\bsecret\s*[:=]\s*\S{4,}'),
    "token_field":      re.compile(r'(?i)\btoken\s*[:=]\s*[A-Za-z0-9_.\-]{8,}'),
    "api_key_field":    re.compile(r'(?i)\bapi[_\-]?key\s*[:=]\s*\S{8,}'),
}


def scan(text: str) -> list[PiiMatch]:
    """
    Scan text for all known PII/secret patterns.

    Returns a list of PiiMatch objects. An empty list means the text is clean.
    Callers should call is_clean() when they only need a boolean check.
    """
    matches: list[PiiMatch] = []
    for pii_type, pattern in _PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append(PiiMatch(
                pii_type=pii_type,
                start=m.start(),
                end=m.end(),
                snippet=m.group()[:20],
            ))
    return matches


def is_clean(text: str) -> bool:
    """Return True if no PII or secrets are detected in text."""
    for pattern in _PATTERNS.values():
        if pattern.search(text):
            return False
    return True


def detected_types(text: str) -> list[str]:
    """Return the unique set of PII type names found (empty if clean)."""
    types = set()
    for pii_type, pattern in _PATTERNS.items():
        if pattern.search(text):
            types.add(pii_type)
    return sorted(types)
