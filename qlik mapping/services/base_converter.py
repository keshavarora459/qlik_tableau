"""
Shared conversion machinery — ported from vl-q2f-mapping/src/converters/base.py.

Every converter in mapping (2) can import from here for centralized retry,
caching, ConversionContext, ConvertedItem, and 4-tier JSON extraction.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Bump this whenever any rule or prompt changes so cached answers are invalidated
PROMPT_VERSION = "2026.09.1"


# ── JSON Utilities ────────────────────────────────────────────────────────────

def _sanitize_json_text(text: str) -> str:
    """Repair common LLM JSON quirks before parsing."""
    if not text:
        return ""
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    # Remove single-line comments // ...
    text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
    # Fix raw unquoted escaped strings like "format_string": \"0.0000000000\"
    text = re.sub(r':\s*\\\"(.*?)\\\"(?=\s*[,}\]])', r': "\1"', text)
    # Fix python booleans/None
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b", "null", text)
    # Fix trailing commas before closing braces or brackets
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def extract_json(raw: str) -> Optional[Any]:
    """
    Pull a JSON value out of a model reply using 4 fallback tiers.
    1. Fenced ```json block
    2. Direct JSON loads
    3. Outermost brace/bracket pair
    4. Unclosed fence
    Returns None if nothing parseable is present.
    """
    if not raw or not str(raw).strip():
        return None

    text = str(raw).strip()

    # Tier 1: First look for full fenced block
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
        for attempt in (candidate, _sanitize_json_text(candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass

    # Tier 2: Try parsing full text directly
    for attempt in (text, _sanitize_json_text(text)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass

    # Tier 3: Fall back to the outermost brace/bracket pair
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            for attempt in (candidate, _sanitize_json_text(candidate)):
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    continue

    # Tier 4: Handle unclosed fence starting with ```json or ```
    unclosed = re.search(r"```(?:json)?\s*([{\[].*)$", text, re.DOTALL | re.IGNORECASE)
    if unclosed:
        candidate = unclosed.group(1).strip()
        for attempt in (candidate, _sanitize_json_text(candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass

    return None


# ── Context & Result Dataclasses ──────────────────────────────────────────────

@dataclass
class ConversionContext:
    """Everything a converter may need beyond the item itself."""
    tables: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    connections: List[Dict[str, Any]] = field(default_factory=list)
    app_id: Optional[str] = None
    workspace_id: Optional[str] = None
    run_id: Optional[str] = None
    is_direct_query: bool = False
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], **overrides) -> "ConversionContext":
        return cls(
            tables=payload.get("tables") or [],
            relationships=payload.get("relationships") or [],
            connections=payload.get("connections") or [],
            app_id=payload.get("app_id"),
            workspace_id=payload.get("workspace_id"),
            run_id=payload.get("run_id"),
            **overrides,
        )


@dataclass
class ConvertedItem:
    """One converted artifact, plus its confidence and status metadata."""
    name: str
    source: Dict[str, Any]
    fabric: Dict[str, Any]
    confidence: Dict[str, Any]
    converted: bool = True
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            **self.source,
            "fabric": self.fabric,
            "confidence": self.confidence,
        }
        if isinstance(self.fabric, dict):
            for key in ("table", "table_name", "mquery", "native_sql", "dax_expression"):
                if key in self.fabric and key not in out:
                    out[key] = self.fabric[key]
        if not self.converted:
            out["converted"] = False
        if self.error:
            out["error"] = self.error
        return out


# ── Base Converter ────────────────────────────────────────────────────────────

class BaseConverter:
    """
    Base class providing retry, caching, and JSON extraction.
    Subclasses supply domain-specific prompt generation and reply handling.
    """
    name: str = "base"
    max_retries: int = 3
    retry_delay: float = 1.0

    def __init__(self, cache: Optional[Dict[str, Any]] = None):
        self.cache = cache if cache is not None else {}
        self.logger = logging.getLogger(f"{__name__}.{self.name}")

    def _cache_key(self, prompt: str) -> str:
        payload = f"{PROMPT_VERSION}:{self.name}:{prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def call_with_retry(self, call_llm_fn, prompt: str, system: str = "") -> Optional[Any]:
        """
        Calls call_llm_fn with retry and caching.
        Returns parsed JSON or None if unparseable / failed.
        """
        key = self._cache_key(prompt)
        if key in self.cache:
            self.logger.debug("Cache hit for %s", self.name)
            return self.cache[key]

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = call_llm_fn(prompt, system=system) if system else call_llm_fn(prompt)
                parsed = extract_json(raw)
                if parsed is not None:
                    self.cache[key] = parsed
                    return parsed
                self.logger.warning("[%s] Attempt %d/%d: JSON extraction failed", self.name, attempt, self.max_retries)
            except Exception as exc:
                last_error = exc
                self.logger.warning("[%s] Attempt %d/%d: Exception: %s", self.name, attempt, self.max_retries, exc)

            if attempt < self.max_retries:
                time.sleep(self.retry_delay)

        self.logger.error("[%s] All %d attempts failed. Last error: %s", self.name, self.max_retries, last_error)
        return None
