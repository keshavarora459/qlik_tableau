"""
Dead-letter store for failed Cosmos DB writes.

When a log/activity/error write to Cosmos fails, the payload is
persisted locally so it can be retried later instead of being silently lost.

The store uses a simple JSON-lines file. A background sweep task
retries dead-lettered entries periodically.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DEAD_LETTER_DIR = os.getenv("DEAD_LETTER_DIR", "/tmp/dead_letters")
_DEAD_LETTER_FILE = os.path.join(_DEAD_LETTER_DIR, "dead_letters.jsonl")
_MAX_ENTRIES = 1000
_RETRY_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Write to dead-letter store
# ---------------------------------------------------------------------------
def write_dead_letter(
    target_url: str,
    payload: dict,
    error: str,
    source_function: str = "unknown",
) -> None:
    """
    Persist a failed write to the dead-letter file.
    Never raises — this is the last-resort fallback.
    """
    try:
        Path(_DEAD_LETTER_DIR).mkdir(parents=True, exist_ok=True)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target_url": target_url,
            "payload": payload,
            "error": str(error),
            "source_function": source_function,
            "retry_count": 0,
        }

        with open(_DEAD_LETTER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")

        # Trim if over max entries
        _trim_dead_letter_file()

    except Exception as e:
        # Absolute last resort — if even the dead-letter store fails,
        # print to stderr so container logs still capture it.
        print(
            f"[DEAD_LETTER_FAILURE] Could not write dead letter: {e} | "
            f"Original error: {error} | Target: {target_url}",
            flush=True,
        )


def _trim_dead_letter_file() -> None:
    """Keep only the most recent _MAX_ENTRIES entries."""
    try:
        if not os.path.exists(_DEAD_LETTER_FILE):
            return

        with open(_DEAD_LETTER_FILE, encoding="utf-8") as f:
            lines = f.readlines()

        if len(lines) > _MAX_ENTRIES:
            # Keep the newest entries
            with open(_DEAD_LETTER_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-_MAX_ENTRIES:])

    except Exception:
        pass  # Trimming is best-effort


# ---------------------------------------------------------------------------
# Read and clear dead-letter entries
# ---------------------------------------------------------------------------
def read_dead_letters() -> list[dict]:
    """Read all dead-letter entries. Returns empty list if file doesn't exist."""
    try:
        if not os.path.exists(_DEAD_LETTER_FILE):
            return []

        entries = []
        with open(_DEAD_LETTER_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    except Exception:
        return []


def clear_dead_letters() -> None:
    """Remove all dead-letter entries after successful retry."""
    try:
        if os.path.exists(_DEAD_LETTER_FILE):
            os.remove(_DEAD_LETTER_FILE)
    except Exception:
        pass


def get_dead_letter_count() -> int:
    """Return the number of pending dead-letter entries."""
    return len(read_dead_letters())
