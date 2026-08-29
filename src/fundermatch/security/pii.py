"""Local redaction used before human-reviewed cases enter precedent memory."""

from __future__ import annotations

import re

_PATTERNS = (
    re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"),
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:\+91[- ]?)?[6-9]\d{9}(?!\d)"),
    re.compile(
        r"\b(?:registered\s+office|residential\s+address|address)\s*[:\-]\s*[^\n]{5,250}",
        re.IGNORECASE,
    ),
    re.compile(r"(?i)\b(?:password|secret|api[_ -]?key|token)\s*[:=]\s*\S+"),
)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern in _PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
