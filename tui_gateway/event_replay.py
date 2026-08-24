"""Per-session event sequencing + bounded replay for WS reconnects.

Every gateway event frame that flows through :func:`server.write_json` (and
therefore ``_emit``) is stamped with a per-session monotonic ``seq`` and
appended to a small ring buffer keyed by session id. A reconnecting client
calls the ``session.events.since`` RPC with its last observed seq; the server
replays everything newer from the buffer, then live events resume seamlessly.

Design constraints honored:
- stdio TUI path unaffected: frames gain a ``seq`` field only on event frames;
  Ink ignores unknown params keys.
- Thread safety: a single module lock guards counters + buffers, so buffer
  order always matches seq order. Wire order is enforced separately by the
  per-transport write path; two racing writers can briefly invert seq order
  on the wire, which the client tolerates (watermarks are monotonic-max).
- Memory bound: _REPLAY_BUFFER_MAX events / _REPLAY_SESSIONS_MAX sessions,
  least-recently-active session evicted first.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

# Replay ring per session. A long turn emits ~hundreds of token events; this
# covers several minutes of streaming plus all control events.
_REPLAY_BUFFER_MAX = 512
# Distinct sessions remembered. Desktop users rarely exceed a dozen live chats.
_REPLAY_SESSIONS_MAX = 64

_replay_lock = threading.Lock()
# sid -> deque of (seq, params_dict). params is the same dict written to the
# wire and already carries its stamped "seq" key — the replay RPC returns
# these bare event objects, matching what the client's live dispatch sees.
_replay_buffers: "OrderedDict[str, deque]" = OrderedDict()
_replay_next_seq: dict[str, int] = {}


def stamp_event(obj: dict) -> None:
    """Stamp one outgoing event frame (mutates obj in place) and record it."""
    if obj.get("method") != "event":
        return
    params = obj.get("params")
    if not isinstance(params, dict):
        return
    sid = params.get("session_id") or ""
    if not sid:
        # Session-less global events (skin.changed etc.) are re-fetchable via
        # their own RPCs; no replay contract for them.
        return
    with _replay_lock:
        seq = _replay_next_seq.get(sid, 0) + 1
        _replay_next_seq[sid] = seq
        params["seq"] = seq
        buf = _replay_buffers.get(sid)
        if buf is None:
            buf = deque(maxlen=_REPLAY_BUFFER_MAX)
            _replay_buffers[sid] = buf
            while len(_replay_buffers) > _REPLAY_SESSIONS_MAX:
                _oldest_sid, _oldest_buf = _replay_buffers.popitem(last=False)
                _replay_next_seq.pop(_oldest_sid, None)
        else:
            # LRU, not insertion-FIFO: an actively streaming session must not
            # be evicted just because it was created before idle newer ones.
            _replay_buffers.move_to_end(sid)
        buf.append((seq, params))


def events_since(sid: str, last_seen: int) -> tuple[list[dict], int, bool]:
    """Replay contract for one session, computed atomically.

    Returns ``(events, latest_seq, truncated)``:

    - ``events``: bare event params dicts (``type``/``session_id``/``seq``/
      ``payload``) with ``seq > last_seen``, in seq order.
    - ``latest_seq``: current highest stamped seq (0 when unknown).
    - ``truncated``: the client cannot trust the gap is fully covered —
      either events between ``last_seen`` and the buffer start were evicted,
      or ``last_seen`` is AHEAD of ``latest_seq`` (seq epoch reset after a
      gateway restart / session eviction). Clients must realign on this flag
      instead of silently accepting a hole.
    """
    sid = sid or ""
    with _replay_lock:
        latest = _replay_next_seq.get(sid, 0)
        buf = _replay_buffers.get(sid)
        if not buf:
            return [], latest, last_seen > latest
        frames = [params for seq, params in buf if seq > last_seen]
        truncated = last_seen > latest or last_seen + 1 < buf[0][0]
        return frames, latest, truncated


def latest_seq(sid: str) -> int:
    """Current highest stamped seq for *sid* (0 when unknown)."""
    with _replay_lock:
        return _replay_next_seq.get(sid or "", 0)


def reset_replay_state() -> None:
    """Test hook."""
    with _replay_lock:
        _replay_buffers.clear()
        _replay_next_seq.clear()


def replay_stats() -> dict:
    """Telemetry: buffer occupancy + per-turn timing for the ops/debug surface."""
    with _replay_lock:
        stats = {
            "sessions": len(_replay_buffers),
            "events": sum(len(b) for b in _replay_buffers.values()),
            "max_per_session": _REPLAY_BUFFER_MAX,
            "max_sessions": _REPLAY_SESSIONS_MAX,
        }
    # Per-turn timing from the session store (not under the replay lock —
    # the session store has its own lock).
    try:
        from tui_gateway.server import _sessions, _sessions_lock
        with _sessions_lock:
            active_turns = []
            for sid, session in _sessions.items():
                inflight = session.get("inflight_turn")
                if isinstance(inflight, dict) and inflight.get("started_at"):
                    active_turns.append({
                        "session_id": sid,
                        "trace_id": inflight.get("trace_id"),
                        "elapsed_s": round(time.time() - inflight["started_at"], 2),
                        "streaming": inflight.get("streaming", False),
                    })
        stats["active_turns"] = active_turns
    except Exception:
        stats["active_turns"] = []
    return stats
