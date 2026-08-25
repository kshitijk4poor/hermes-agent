"""Session store for tui_gateway — the authoritative in-memory session table.

Extracted from ``tui_gateway/server.py`` (Phase 2 of #94484) to create a clean
boundary between the session lifecycle (owned here) and the RPC dispatch +
event emission (owned by server.py). The gateway core can eventually import
this module directly to share session state with platform adapters.

Design:
- ``sessions`` is the same ``dict[str, dict]`` server.py has always used.
- ``lock`` is the same ``threading.RLock`` (reentrant — callers like
  ``_close_session_by_id`` may run under callers that already hold it).
- Helper methods (``get``, ``pop``, ``items``, ``values``, ``keys``,
  ``len``) are thin wrappers that acquire the lock for read operations.
  Write operations (``set``, ``pop``) are NOT auto-locked — callers must
  hold ``with lock:`` for atomic check-and-set, matching the existing
  contract where every caller already does ``with _sessions_lock:``.

This is deliberately a thin extraction: the 150 session-related functions in
server.py still live there, but they import ``sessions`` and ``lock`` from
this module instead of declaring them at module level. A later phase moves
the functions themselves.
"""

from __future__ import annotations

import threading
from typing import Any, Iterator


# ── The session table ────────────────────────────────────────────────────

sessions: dict[str, dict] = {}

# Reentrant: _close_session_by_id may run under callers that already hold it.
lock = threading.RLock()


# ── Thread-safe read helpers ─────────────────────────────────────────────
# These cover the common "look up a session without modifying it" pattern.
# Callers that need atomic check-and-set still use `with lock:` directly.

def get(sid: str) -> dict | None:
    """Return the session dict for *sid*, or None."""
    with lock:
        return sessions.get(sid)


def items() -> list[tuple[str, dict]]:
    """Snapshot of all (sid, session) pairs."""
    with lock:
        return list(sessions.items())


def values() -> list[dict]:
    """Snapshot of all session dicts."""
    with lock:
        return list(sessions.values())


def keys() -> list[str]:
    """Snapshot of all session ids."""
    with lock:
        return list(sessions.keys())


def __len__() -> int:
    """Number of live sessions."""
    with lock:
        return len(sessions)


def __contains__(sid: str) -> bool:
    """True if *sid* is a live session."""
    with lock:
        return sid in sessions


# ── Write helpers (caller must hold lock for atomicity) ─────────────────

def set(sid: str, session: dict) -> None:
    """Insert or replace a session. Caller should hold ``lock`` for
    atomic check-and-set sequences."""
    sessions[sid] = session


def pop(sid: str) -> dict | None:
    """Remove and return a session, or None. Caller should hold ``lock``."""
    return sessions.pop(sid, None)


def clear() -> None:
    """Remove all sessions. Caller should hold ``lock``."""
    sessions.clear()
