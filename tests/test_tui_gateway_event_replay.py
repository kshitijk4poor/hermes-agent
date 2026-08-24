"""Tests for tui_gateway.event_replay — per-session event seq + replay ring."""

import threading

import pytest

from tui_gateway import event_replay
from tui_gateway.event_replay import (
    events_since,
    latest_seq,
    replay_stats,
    reset_replay_state,
    stamp_event,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_replay_state()
    yield
    reset_replay_state()


def _frame(sid, etype="message.delta"):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": etype, "session_id": sid, "payload": {}},
    }


def test_stamp_adds_monotonic_seq_per_session():
    f1 = _frame("s1")
    f2 = _frame("s1")
    other = _frame("s2")

    stamp_event(f1)
    stamp_event(other)
    stamp_event(f2)

    assert f1["params"]["seq"] == 1
    assert f2["params"]["seq"] == 2  # per-session counter, unaffected by s2
    assert other["params"]["seq"] == 1


def test_stamp_ignores_non_event_and_sessionless_frames():
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {}}
    no_sid = {"jsonrpc": "2.0", "method": "event", "params": {"type": "skin.changed"}}

    stamp_event(rpc)
    stamp_event(no_sid)

    assert "seq" not in rpc
    assert "seq" not in no_sid["params"]
    assert replay_stats()["events"] == 0


def test_events_since_returns_bare_params_only_newer_in_order():
    frames = [_frame("s1") for _ in range(5)]
    for f in frames:
        stamp_event(f)

    got, latest, truncated = events_since("s1", 3)
    assert [e["seq"] for e in got] == [4, 5]
    # Replay returns the bare event params (what live dispatch sees), NOT the
    # full JSON-RPC frame envelope — the client reads event.type at top level.
    assert all("jsonrpc" not in e and e["type"] == "message.delta" for e in got)
    assert latest == 5
    assert truncated is False

    all_events, _, _ = events_since("s1", 0)
    assert all_events == [f["params"] for f in frames]
    assert events_since("s1", 5) == ([], 5, False)
    assert latest_seq("s1") == 5


def test_unknown_session_returns_empty():
    assert events_since("nope", 0) == ([], 0, False)
    assert latest_seq("nope") == 0


def test_ring_buffer_is_bounded_and_reports_truncation():
    for _ in range(event_replay._REPLAY_BUFFER_MAX + 50):
        stamp_event(_frame("s1"))

    stats = replay_stats()
    assert stats["events"] == event_replay._REPLAY_BUFFER_MAX

    # Client that saw nothing (last_seen=0) has a gap older than the ring.
    got, latest, truncated = events_since("s1", 0)
    assert truncated is True
    assert latest == event_replay._REPLAY_BUFFER_MAX + 50
    assert len(got) == event_replay._REPLAY_BUFFER_MAX

    # Client aligned with the buffer start: fully covered, not truncated.
    oldest = got[0]["seq"]
    covered, _, covered_truncated = events_since("s1", oldest - 1)
    assert covered_truncated is False
    assert len(covered) == event_replay._REPLAY_BUFFER_MAX


def test_epoch_reset_reports_truncated():
    """Client watermark AHEAD of the server (gateway restart) must be flagged.

    Without this, a restarted server returns [] / truncated=False and the
    client's stuck watermark silently kills replay forever.
    """
    for _ in range(3):
        stamp_event(_frame("s1"))

    got, latest, truncated = events_since("s1", 97)
    assert got == []
    assert latest == 3
    assert truncated is True

    # Session the server has never seen but the client has a watermark for.
    assert events_since("gone", 42) == ([], 0, True)


def test_session_count_bounded_with_lru_eviction():
    for i in range(event_replay._REPLAY_SESSIONS_MAX + 10):
        stamp_event(_frame(f"s{i}"))

    stats = replay_stats()
    assert stats["sessions"] == event_replay._REPLAY_SESSIONS_MAX
    assert events_since("s0", 0)[0] == []  # oldest session fully evicted
    assert latest_seq(f"s{event_replay._REPLAY_SESSIONS_MAX + 9}") == 1


def test_active_session_survives_eviction_lru():
    """Eviction is least-recently-ACTIVE, not first-created: a session that
    keeps streaming must outlive idle sessions created after it."""
    stamp_event(_frame("active"))
    for i in range(event_replay._REPLAY_SESSIONS_MAX - 1):
        stamp_event(_frame(f"idle{i}"))

    # "active" is now the oldest by creation. Touch it, then overflow.
    stamp_event(_frame("active"))
    stamp_event(_frame("newcomer"))

    assert latest_seq("active") == 2  # survived — it was most recently active
    assert events_since("idle0", 0) == ([], 0, False)  # idle0 evicted instead


def test_concurrent_stamping_never_drops_or_duplicates_seq():
    errors = []

    def worker(sid):
        try:
            seen = set()
            for _ in range(200):
                f = _frame(sid)
                stamp_event(f)
                seq = f["params"]["seq"]
                assert seq not in seen
                seen.add(seq)
        except AssertionError as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert replay_stats()["events"] == 8 * 200
