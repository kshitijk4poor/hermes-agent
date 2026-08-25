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
import time
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

def put(sid: str, session: dict) -> None:
    """Insert or replace a session. Caller should hold ``lock`` for
    atomic check-and-set sequences."""
    sessions[sid] = session


def pop(sid: str) -> dict | None:
    """Remove and return a session, or None. Caller should hold ``lock``."""
    return sessions.pop(sid, None)


def clear() -> None:
    """Remove all sessions. Caller should hold ``lock``."""
    sessions.clear()


# ── Session field helpers ────────────────────────────────────────────────
# These operate on a session dict (returned by get/create) and do NOT need
# the store lock — the caller holds a reference to the dict and mutates it
# under the session's own history_lock or the store lock as appropriate.

def is_running(session: dict | None) -> bool:
    """True if the session has an in-flight agent turn."""
    return bool((session or {}).get("running"))


def inflight_turn(session: dict | None) -> dict | None:
    """Return the inflight turn dict, or None when idle."""
    turn = (session or {}).get("inflight_turn")
    return turn if isinstance(turn, dict) else None


def trace_id(session: dict | None) -> str | None:
    """Return the current turn's trace_id, or None when idle."""
    turn = inflight_turn(session)
    return turn.get("trace_id") if turn else None


def turn_elapsed(session: dict | None) -> float | None:
    """Seconds since the current turn started, or None when idle."""
    turn = inflight_turn(session)
    if not turn or not turn.get("started_at"):
        return None
    return round(time.time() - float(turn["started_at"]), 2)


def active_turns() -> list[dict]:
    """Snapshot of all sessions with in-flight turns, with trace_id + elapsed.

    Used by ``session.events.stats`` telemetry and the ops/debug surface.
    """
    with lock:
        result = []
        for sid, session in sessions.items():
            turn = inflight_turn(session)
            if turn and turn.get("started_at"):
                result.append({
                    "session_id": sid,
                    "trace_id": turn.get("trace_id"),
                    "elapsed_s": round(time.time() - float(turn["started_at"]), 2),
                    "streaming": turn.get("streaming", False),
                })
        return result


def live_session_ids() -> list[str]:
    """All session ids this process holds in memory.

    Includes both the UI session id (``sid``) and the agent's session_key /
    session_id for each live session, so callers can exclude every identity
    a live row might use from a DB sweep.
    """
    ids: set[str] = set()
    with lock:
        for sid, session in sessions.items():
            if sid:
                ids.add(str(sid))
            agent = session.get("agent") if isinstance(session, dict) else None
            for candidate in (
                getattr(agent, "session_id", None),
                session.get("session_key") if isinstance(session, dict) else None,
            ):
                if candidate:
                    ids.add(str(candidate))
    return sorted(ids)
