"""Per-run accounting of what the LLM actually did.

`SummaryBuilder.build_llm_status()` used to hardcode `converted: True` and
name the configured Groq model regardless of whether a single request was
ever sent - so a mapping produced entirely by the regex/lookup fallbacks
still advertised itself as LLM-converted. Anything downstream (and anyone
reading the stored mapping document) had no way to tell the two apart.

A ContextVar rather than a module global: the service handles concurrent
runs, and a plain global would mix one request's counters into another's.
Each `/api/mapping` call starts a fresh tracker via `start_run()`.
"""

import contextvars
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class LLMUsage:
    """Counts one mapping run's LLM activity."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    rate_limited: int = 0
    cache_hits: int = 0
    deterministic_conversions: int = 0
    fallback_conversions: int = 0
    # Model output accepted over the deterministic baseline.
    accepted: int = 0
    # Model answered, but validation rejected it and the baseline was kept.
    rejected: int = 0
    by_stage: Dict[str, Dict[str, int]] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)

    def _stage(self, stage: str) -> Dict[str, int]:
        return self.by_stage.setdefault(
            stage,
            {
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "rate_limited": 0,
                "cache_hits": 0,
                "deterministic_conversions": 0,
                "fallback_conversions": 0,
                "accepted": 0,
                "rejected": 0,
            },
        )

    def record_attempt(self, stage: str) -> None:
        self.attempted += 1
        self._stage(stage)["attempted"] += 1

    def record_success(self, stage: str) -> None:
        self.succeeded += 1
        self._stage(stage)["succeeded"] += 1

    def record_cache_hit(self, stage: str) -> None:
        self.cache_hits += 1
        self._stage(stage)["cache_hits"] = self._stage(stage).get("cache_hits", 0) + 1

    def record_deterministic(self, stage: str) -> None:
        self.deterministic_conversions += 1
        self._stage(stage)["deterministic_conversions"] = self._stage(stage).get("deterministic_conversions", 0) + 1

    def record_fallback(self, stage: str) -> None:
        self.fallback_conversions += 1
        self._stage(stage)["fallback_conversions"] = self._stage(stage).get("fallback_conversions", 0) + 1

    def record_rate_limited(self, stage: str, reason: str = "429 rate limit") -> None:
        self.rate_limited += 1
        self._stage(stage)["rate_limited"] = self._stage(stage).get("rate_limited", 0) + 1
        if reason and len(self.failures) < 20:
            self.failures.append(f"{stage} [rate_limited]: {reason}"[:300])

    def record_failure(self, stage: str, reason: str = "") -> None:
        self.failed += 1
        self._stage(stage)["failed"] += 1
        if reason and len(self.failures) < 20:
            self.failures.append(f"{stage}: {reason}"[:300])

    def record_accepted(self, stage: str) -> None:
        self.accepted += 1
        self._stage(stage)["accepted"] += 1

    def record_rejected(self, stage: str, reason: str = "") -> None:
        self.rejected += 1
        self._stage(stage)["rejected"] += 1
        if reason and len(self.failures) < 20:
            self.failures.append(f"{stage} rejected: {reason}"[:300])

    def as_dict(self) -> Dict[str, object]:
        return {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "rate_limited": self.rate_limited,
            "cache_hits": self.cache_hits,
            "deterministic_conversions": self.deterministic_conversions,
            "fallback_conversions": self.fallback_conversions,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "by_stage": self.by_stage,
            "failures": self.failures,
        }


_CURRENT: contextvars.ContextVar[LLMUsage] = contextvars.ContextVar("llm_usage")


def start_run() -> LLMUsage:
    """Begin accounting for a new mapping run and return its tracker."""
    usage = LLMUsage()
    _CURRENT.set(usage)
    return usage


def current() -> LLMUsage:
    """The active run's tracker.

    Falls back to a detached tracker rather than raising, so a converter
    used outside a request (tests, scripts) still works - it just records
    into an object nobody reads.
    """
    try:
        return _CURRENT.get()
    except LookupError:
        usage = LLMUsage()
        _CURRENT.set(usage)
        return usage
