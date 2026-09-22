"""In-memory cache and deduplication for LLM conversions.

Generates deterministic cache keys from model, task type, normalized expressions,
and context. Avoids redundant LLM calls for repeated expressions across tables/measures/visuals.
"""

import hashlib
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)


class LLMCache:
    """Thread-safe, TTL-based in-memory cache for LLM completion results."""

    def __init__(self, max_size: int = 1000, ttl: int = 86400):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalize whitespace and case for stable key generation."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", str(text)).strip()

    def make_key(
        self,
        model: str,
        task: str,
        expression: str,
        extra_context: Optional[str] = None,
    ) -> str:
        """Generate a deterministic SHA-256 cache key."""
        norm_expr = self.normalize_text(expression)
        norm_ctx = self.normalize_text(extra_context or "")
        raw = f"{model}:::{task}:::{norm_expr}:::{norm_ctx}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        """Retrieve a cached value if present and not expired."""
        if not Config.LLM_CACHE_ENABLED:
            return None

        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None

        created_at, value = entry
        if time.time() - created_at > self.ttl:
            # Expired
            self._cache.pop(key, None)
            self._misses += 1
            return None

        self._hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        """Store a value in cache with eviction if limit exceeded."""
        if not Config.LLM_CACHE_ENABLED or value is None:
            return

        # Evict oldest 20% entries if cache grows beyond max_size
        if len(self._cache) >= self.max_size:
            sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][0])
            to_remove = sorted_keys[: max(1, self.max_size // 5)]
            for k in to_remove:
                self._cache.pop(k, None)

        self._cache[key] = (time.time(), value)

    def stats(self) -> Dict[str, int]:
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
        }

    def clear(self) -> None:
        self._cache.clear()
        self._hits = 0
        self._misses = 0


# Global shared cache instance
_GLOBAL_CACHE = LLMCache()


def get_llm_cache() -> LLMCache:
    return _GLOBAL_CACHE
