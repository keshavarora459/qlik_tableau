import re
from typing import Any

_SENSITIVE_PATTERNS = [
    (re.compile(r'(gsk_[A-Za-z0-9_]{32,})'), '[REDACTED_API_KEY]'),
    (re.compile(r'(sk-[A-Za-z0-9_]{32,})'), '[REDACTED_API_KEY]'),
    (re.compile(r'(Bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*', re.IGNORECASE), r'\1[REDACTED_TOKEN]'),
    (re.compile(r'(password|secret|api_key|token)["\']?\s*[:=]\s*["\']?([^"\'\s]+)', re.IGNORECASE), r'\1: [REDACTED]'),
]


def redact_sensitive_data(data: Any) -> Any:
    """Redact sensitive information (API keys, tokens, credentials) from logs and error messages."""
    if data is None:
        return ""
    text = str(data)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
