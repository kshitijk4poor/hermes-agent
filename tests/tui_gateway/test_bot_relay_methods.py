"""Tests: bot_relay.* JSON-RPC handlers (tui_gateway/methods_bot_relay.py).

The Desktop's relay door on each connected gateway. Contracts:
- roster.sync persists validated rows and reports the accepted count;
- outbox.drain replays a canonical envelope (same id) until its reply lands;
- deliver requires a stable envelope id, resolves the target profile's home
  on THIS install and forwards to that profile's authority — never a CLI turn;
- reply writes the waiter's file and rejects malformed envelope ids.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import tui_gateway.server as srv
from hermes_cli.dashboard_auth.ws_tickets import INTERNAL_PROVIDER, INTERNAL_USER_ID
from tools import bot_relay
from tui_gateway import methods_bot_relay


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    (h / "profiles" / "ops").mkdir(parents=True)
    (h / "profiles" / "ops" / "config.yaml").write_text("{}\n")  # identity marker: a bare dir is no target
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _result(envelope):
    assert "error" not in envelope, envelope
    return envelope["result"]


def test_roster_sync_persists_and_counts(home):
    out = _result(
        srv._methods["bot_relay.roster.sync"](
            1,
            {
                "agents": [
                    {"profile": "scout", "handle": "scout", "connection_id": "cloud-1"},
                    {"profile": "", "connection_id": "cloud-1"},  # dropped
                ]
            },
        )
    )
    assert out["count"] == 1
    assert [r["profile"] for r in bot_relay.read_remote_roster(home)] == ["scout"]


def test_outbox_drain_replays_canonical_envelope_until_reply_acknowledged(home):
    """A drained envelope is not "delivered" — its stable id is replayed on every
    drain until the target's terminal reply is written under that id, so a
    Desktop that lost the first drain result cannot drop the DM."""
    target = {"profile": "scout", "handle": "scout", "connection_id": "cloud-1",
              "connection_label": "", "title": "", "description": ""}
    env = bot_relay.enqueue_envelope(
        home, target=target, message="m", sender_profile="default", sender_handle="hermes"
    )
    first = _result(srv._methods["bot_relay.outbox.drain"](1, {}))
    assert [e["id"] for e in first["envelopes"]] == [env["id"]]
    second = _result(srv._methods["bot_relay.outbox.drain"](2, {}))
    assert [e["id"] for e in second["envelopes"]] == [env["id"]], "same id, never a duplicate envelope"
    assert second["envelopes"][0]["message"] == "m"
    _result(srv._methods["bot_relay.reply"](3, {"id": env["id"], "reply": "done"}))
    assert _result(srv._methods["bot_relay.outbox.drain"](4, {}))["envelopes"] == []


def test_deliver_forwards_stable_id_to_target_profile_authority(home, monkeypatch):
    """The relay door is a transport bridge: it resolves the target profile's
    own home and forwards the envelope id + message to THAT authority. It
    never runs a CLI turn of its own."""
    from tools import bot_live_delivery as live

    forwarded = []
    spawned = []

    def _fake_run(argv, *a, **k):
        # The server module's import-time update prefetch runs `git ...`; only
        # a `hermes` spawn would be a delivery attempt.
        if argv and argv[0] != "git":
            spawned.append(argv)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _fake_authority(target_home, params):
        forwarded.append((target_home, dict(params)))
        return {"status": "queued", "delivery_id": params["id"], "reply": ""}

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(live, "authority_delivery", _fake_authority)
    envelope_id = "b" * 32
    out = _result(srv._methods["bot_relay.deliver"](1, {"id": envelope_id, "profile": "ops", "message": "ping"}))
    assert out["status"] == "queued" and out["delivery_id"] == envelope_id
    assert forwarded[-1][0] == home / "profiles" / "ops"
    assert forwarded[-1][1]["id"] == envelope_id and forwarded[-1][1]["profile"] == "ops"

    # Exact retry carries the SAME id to the same authority — no second envelope.
    _result(srv._methods["bot_relay.deliver"](2, {"id": envelope_id, "profile": "ops", "message": "ping"}))
    assert [p["id"] for _h, p in forwarded] == [envelope_id, envelope_id]

    # 'hermes' alias resolves to the default profile's home.
    _result(srv._methods["bot_relay.deliver"](3, {"id": "c" * 32, "profile": "hermes", "message": "x"}))
    assert forwarded[-1][0] == home and forwarded[-1][1]["profile"] == "default"
    assert not spawned


def test_deliver_unreachable_authority_is_a_typed_refusal(home, monkeypatch):
    from tools import bot_live_delivery as live

    def _down(target_home, params):
        raise ValueError("profile authority is not ready")

    monkeypatch.setattr(live, "authority_delivery", _down)
    err = srv._methods["bot_relay.deliver"](1, {"id": "d" * 32, "profile": "ghost", "message": "x"})
    assert err["error"]["data"]["reason"] == "runtime_unavailable"
    assert "not ready" in err["error"]["message"]


@pytest.mark.parametrize("params", [
    {"profile": "", "message": ""},
    {"profile": "ops", "message": "no envelope id"},
    {"id": "../evil", "profile": "ops", "message": "x"},
    {"id": "e" * 32, "profile": "../ops", "message": "x"},
])
def test_deliver_requires_stable_id_and_valid_profile(home, monkeypatch, params):
    from tools import bot_live_delivery as live

    monkeypatch.setattr(live, "authority_delivery",
                        lambda *a, **k: pytest.fail("malformed requests never reach an authority"))
    err = srv._methods["bot_relay.deliver"](1, params)
    assert err["error"]["data"]["reason"] == "invalid_params"


def _lease_open_bot_chat(home, *, live_session_id="live-in-other-process"):
    """A Bot Chat leased by a mailbox-capable live owner in the target's home (real state.db row,
    real lease) — what a Desktop-opened Bot Chat looks like from the relay handler's side."""
    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB

    ops_home = home / "profiles" / "ops"
    db = SessionDB(db_path=ops_home / "state.db")
    db.create_session(session_id="chat", source="desktop")
    db.set_session_title("chat", "Bot Chat")
    db.close()
    lease, refusal = try_acquire_active_session(
        session_id="chat", surface="desktop", config={}, registry_home=ops_home,
        metadata={"live_session_id": live_session_id, "bot_live_delivery_consumer": True})
    assert refusal is None
    return ops_home, lease


def _no_cli_transport(monkeypatch, spawned):
    def _fake_run(argv, *a, **k):
        if argv and argv[0] != "git":
            spawned.append(argv)
        raise AssertionError("the CLI transport collides with the live owner")

    # Guard the deliver child's runner, whichever this tree has: subprocess.run today, and
    # quiet_single_query.run_reported_turn once the relay books turns from their report
    # (#114980) — guarding only the first would let a real ``hermes chat -Q`` child spawn there.
    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr("hermes_cli.quiet_single_query.run_reported_turn", _fake_run, raising=False)


def _owner_settles(ops_home, outcome: dict) -> threading.Thread:
    """Stand in for the owner's poller (session_notifications._poll_bot_live_delivery_once): claim
    the queued DM, run "the turn", write the terminal receipt."""
    from tools import bot_live_delivery as mailbox

    def run():
        owner = mailbox.find_canonical_live_owner(ops_home)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            claimed = mailbox.claim_pending_delivery(ops_home, owner)
            if claimed is not None:
                mailbox.complete_delivery(ops_home, claimed["delivery_id"], **outcome)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize(
    ("live_here", "outcome", "expect"),
    [
        (True, {"status": "settled", "reply": "pong from the open chat"}, ("reply", "pong from the open chat")),
        (False, {"status": "failed", "error": "Error code: 429 - rate limit exceeded", "reason": "provider_rate_limit"},
         ("reason", "provider_rate_limit")),
    ],
    ids=["answer-from-a-chat-live-here", "typed-failure-from-a-chat-live-elsewhere"],
)
def test_deliver_into_an_open_bot_chat_returns_the_owners_answer(home, monkeypatch, live_here, outcome, expect):
    """#113753 put a relayed DM into the mailbox of a Bot Chat live in a sibling process, and the
    owner settles a receipt carrying the reply — the receipt local DMs wait on (_wait_live_dm).
    The relay answered with a receipt sentence instead, so the sending agent never got the target's
    answer whenever its Bot Chat happened to be open. Now the handler waits on the receipt, on the
    local lane's budget: the reply comes back, a failed turn as the typed 5092 refusal, and the
    CLI is never spawned. A mailbox-capable chat live in THIS process takes the same door — one
    door, one receipt — and prompt.submit (#100523) stays the fallback for a live session with no
    mailbox (test_deliver_lands_in_live_bot_chat_instead_of_subprocess)."""
    ops_home, lease = _lease_open_bot_chat(home)
    spawned, submitted = [], []
    _no_cli_transport(monkeypatch, spawned)
    monkeypatch.setitem(
        srv._methods, "prompt.submit", lambda rid, p: submitted.append(p) or srv._ok(rid, {"status": "streaming"}))
    monkeypatch.setattr(srv, "_profile_home", lambda name: ops_home)
    monkeypatch.setattr(srv, "_sessions", (
        {"live-ops": {"profile_home": str(ops_home), "pending_title": "Bot Chat", "history": []}} if live_here else {}))
    monkeypatch.setattr("tools.bot_mode_dm._LIVE_WAIT_SECONDS", 10)
    try:
        owner = _owner_settles(ops_home, outcome)
        out = srv._methods["bot_relay.deliver"](1, {
            "profile": "ops", "message": "ping", "from_profile": "cody", "from_handle": "cody",
            "from_connection": "conn-a"})
        owner.join(timeout=10)
        assert not spawned and submitted == []
        key, value = expect
        if key == "reply":
            assert _result(out)["reply"] == value
        else:
            assert out["error"]["code"] == 5092 and out["error"]["data"]["reason"] == value
    finally:
        lease.release()


def test_deliver_into_a_busy_open_bot_chat_reports_it_queued_and_keeps_the_receipt(home, monkeypatch):
    """The owner admits at its next idle boundary; a DM not answered within the budget is reported
    queued there — the record stays for the owner, carrying the message and the relayed sender as
    the turn author, and the sender is told not to resend."""
    from tools import bot_live_delivery as mailbox

    ops_home, lease = _lease_open_bot_chat(home)
    spawned = []
    _no_cli_transport(monkeypatch, spawned)
    monkeypatch.setattr(srv, "_profile_home", lambda name: ops_home)
    monkeypatch.setattr(srv, "_sessions", {})
    monkeypatch.setattr("tools.bot_mode_dm._LIVE_WAIT_SECONDS", 0.6)
    try:
        out = _result(srv._methods["bot_relay.deliver"](1, {
            "profile": "ops", "message": "ping", "from_profile": "cody", "from_handle": "cody",
            "from_connection": "conn-a"}))
        assert not spawned and "open Bot Chat" in out["reply"] and "Do not resend" in out["reply"]
        (queued,) = [
            r for p in (ops_home / "runtime" / mailbox.DELIVERY_DIR_NAME).glob("*.json")
            if (r := json.loads(p.read_text(encoding="utf-8")))]
        assert queued["status"] == "queued" and queued["message"] == "ping"
        assert queued["owner"]["lease_id"] == lease.lease_id
        assert queued["author"]["name"] == "cody" and queued["author"]["is_bot"] is True
    finally:
        lease.release()


def test_reply_roundtrip_and_id_validation(home):
    envelope_id = "c" * 32
    _result(srv._methods["bot_relay.reply"](1, {"id": envelope_id, "reply": "hi"}))
    path = bot_relay.relay_root(home) / bot_relay.REPLIES_DIR / f"{envelope_id}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["reply"] == "hi"

    err = srv._methods["bot_relay.reply"](2, {"id": "../evil"})
    assert "error" in err


class _Client:
    def __init__(self, auth_identity=None):
        self.auth_identity = auth_identity

    def write(self, obj):
        return True

    def close(self):
        return None


@pytest.fixture
def bound_client(monkeypatch):
    """Bind a fake calling transport for the handler; yields a setter for its ``auth_identity``."""
    client = _Client()
    token = srv.bind_transport(client)
    try:
        yield client
    finally:
        srv.reset_transport(token)


SENDER = {"from_profile": "scout", "from_handle": "scout", "from_connection": "cloud-1"}
SENDER_AUTHOR = {"id": "bot:cloud-1/scout", "name": "scout", "is_bot": True}


@pytest.mark.parametrize("identity, refused", [
    (None, False),
    ({"user_id": INTERNAL_USER_ID, "provider": INTERNAL_PROVIDER}, False),
    ({"user_id": "alice", "provider": "google"}, True),
])
def test_relay_sender_attribution_obeys_transport_identity(home, monkeypatch, bound_client, identity, refused):
    from tools import bot_live_delivery as live
    forwarded = []
    monkeypatch.setattr(live, "authority_delivery",
                        lambda home, params: forwarded.append(params) or {"status": "queued"})
    bound_client.auth_identity = identity
    result = srv._methods["bot_relay.deliver"](1, {
        "id": "f" * 32, "profile": "ops", "message": "ping", **SENDER})
    if refused:
        assert result["error"]["code"] == 4095
        assert not forwarded
    else:
        assert _result(result)["status"] == "queued"
        assert forwarded[0]["author"] == SENDER_AUTHOR
        assert not any(key in forwarded[0] for key in SENDER)


@pytest.mark.parametrize("subdir", ["profiles/ops", "dev"])
def test_gateway_drains_the_mailbox_the_tools_write_to(tmp_path, monkeypatch, subdir):
    """Both ends of the relay mailbox derive the install root from HERMES_HOME with ONE formula.
    The writer side (``message_agent``'s ``_hermes_root``) and the drain side
    (``methods_bot_relay._relay_root``) must agree for a ``profiles/<name>`` home AND for an
    arbitrary subdir of the native ``~/.hermes`` — a split here is silent non-delivery."""
    from tools.bot_mode_probe import _default_home, _hermes_root
    from tui_gateway import methods_bot_relay

    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes" / subdir
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    writer_root = _hermes_root(Path(_default_home()))
    target = {"profile": "scout", "handle": "scout", "connection_id": "cloud-1",
              "connection_label": "", "title": "", "description": ""}
    env = bot_relay.enqueue_envelope(
        writer_root, target=target, message="m", sender_profile="default", sender_handle="hermes")

    assert methods_bot_relay._relay_root() == writer_root
    drained = _result(srv._methods["bot_relay.outbox.drain"](1, {}))
    assert [e["id"] for e in drained["envelopes"]] == [env["id"]]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _child_argv(monkeypatch, body: str) -> dict:
    """Stand in a Python child for the ``hermes`` transport; it imports ``hermes_cli`` from this checkout."""
    argv = [sys.executable, "-c", textwrap.dedent(body)]
    monkeypatch.setattr(bot_relay, "local_delivery_command", lambda prof, tmp: argv)
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (_REPO_ROOT, os.environ.get("PYTHONPATH")) if p)}


def _spy_popen():
    procs, real_popen = [], subprocess.Popen

    def spy(*args, **kwargs):
        procs.append(real_popen(*args, **kwargs))
        return procs[-1]

    return procs, spy


def test_reported_turn_still_lingering_at_the_cap_is_booked_from_its_latest_report_not_killed(tmp_path, monkeypatch):
    """#114980: the cap bounds the TURN. A child that reported its turn and then lingers for a
    nested notify_on_complete reply (bounded by oneshot_completion_wait_seconds, default == the
    cap) is booked at the cap from its report — the answer a follow-up turn last wrote there,
    exit code 0, never delivery_timeout — and is NOT killed, so its own handoff survives."""
    env = _child_argv(monkeypatch, """
        import os, time
        from hermes_cli.quiet_single_query import TURN_REPORT_FILE_ENV, write_turn_report
        path = os.environ.pop(TURN_REPORT_FILE_ENV)
        write_turn_report(path, exit_code=0, reply="asking the teammate")
        time.sleep(0.5)
        write_turn_report(path, exit_code=0, reply="teammate says: done")
        time.sleep(30)
        """)
    procs, spy = _spy_popen()
    tmp = tmp_path / "dm.txt"
    tmp.write_text("hi", encoding="utf-8")
    started = time.monotonic()
    try:
        with mock.patch.object(subprocess, "Popen", side_effect=spy) as popen:
            result = methods_bot_relay._run_delivery("ops", str(tmp), env, timeout=2)
        elapsed = time.monotonic() - started
        assert (result.returncode, result.stdout, result.stderr) == (0, "teammate says: done", "")
        assert 2 <= elapsed < 8, elapsed
        assert procs[0].poll() is None, "the lingering child must survive the booking"
        assert not (tmp_path / "dm.txt.turn.json").exists(), "the report is the runner's to clean up"
        # Decoding stays pinned through the runner (#93590 sibling defect): without encoding= the
        # child's UTF-8 output is decoded with the locale codec — cp1252/GBK on Windows — mangling
        # non-ASCII replies; errors="replace" keeps a bad byte from raising instead of delivering.
        assert popen.call_args.kwargs["encoding"] == "utf-8" and popen.call_args.kwargs["errors"] == "replace"
    finally:
        for proc in procs:
            proc.kill()
            proc.wait(timeout=10)


def test_turn_that_never_ends_is_still_a_delivery_timeout(tmp_path, monkeypatch):
    """Control: with no turn report the cap stays the guard it always was."""
    env = _child_argv(monkeypatch, "import time; time.sleep(30)")
    tmp = tmp_path / "dm.txt"
    tmp.write_text("hi", encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        methods_bot_relay._run_delivery("ops", str(tmp), env, timeout=1)
    assert time.monotonic() - started < 8
