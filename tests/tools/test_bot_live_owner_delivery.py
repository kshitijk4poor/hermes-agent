"""Canonical delivery invariants: stable envelope IDs, authority-only admission,
immutable receipts. The mailbox is receipt storage, never an execution queue."""
import os
from pathlib import Path

import pytest

from tools import bot_live_delivery as mailbox


class _FakeAuthority:
    """Stands in for the profile authority's ``bot_relay.deliver`` RPC."""

    def __init__(self):
        self.calls = []
        self.records = {}

    def __call__(self, home, params):
        self.calls.append((Path(home).resolve(), dict(params)))
        record = self.records.get(params["id"])
        if record is None:
            record = self.records[params["id"]] = dict(
                status="queued", delivery_id=params["id"], profile_home=str(Path(home).resolve()),
                session_id="local-bot", message=params["message"], admission_id="adm-" + params["id"][:6],
                reply="", **({"author": params["author"]} if "author" in params else {}))
            with mailbox._locked(home) as root:
                mailbox._write(root / f"{params['id']}.json", record)
        elif (record["message"] != params["message"]
              or record.get("author") != params.get("author")):
            raise ValueError("admission_conflict")
        return {k: record[k] for k in ("status", "delivery_id", "profile_home", "session_id", "reply")}


def _owner(home):
    return dict(profile_home=str(Path(home).resolve()), session_id="local-bot",
                lease_id="authority-1", live_session_id="local-bot")


def test_same_envelope_id_admits_once_and_conflicts_on_changed_payload(tmp_path, monkeypatch):
    authority = _FakeAuthority()
    monkeypatch.setattr(mailbox, "authority_delivery", authority)
    delivery_id = "a" * 32
    queued = mailbox.deliver_to_live_owner(tmp_path, _owner(tmp_path), "hello", delivery_id=delivery_id)
    assert queued["status"] == "queued" and queued["delivery_id"] == delivery_id
    # Exact retry inspects the same admission; it never mints a second envelope.
    assert mailbox.deliver_to_live_owner(tmp_path, _owner(tmp_path), "hello", delivery_id=delivery_id) == queued
    assert [params["id"] for _home, params in authority.calls] == [delivery_id, delivery_id]
    assert len(authority.records) == 1
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(tmp_path, _owner(tmp_path), "different", delivery_id=delivery_id)
    # Reading the receipt resolves through the same admission, not a local guess.
    assert mailbox.read_delivery_result(tmp_path, delivery_id)["status"] == "queued"
    # The retired UI claim consumer never hands out work.
    assert mailbox.claim_pending_delivery(tmp_path, _owner(tmp_path)) is None
    if os.name != "nt":
        for path in (tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME).iterdir():
            assert path.stat().st_mode & 0o077 == 0


def test_owner_from_another_home_or_malformed_id_is_refused_before_admission(tmp_path, monkeypatch):
    authority = _FakeAuthority()
    monkeypatch.setattr(mailbox, "authority_delivery", authority)
    foreign = dict(_owner(tmp_path), profile_home=str(tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="different profile home"):
        mailbox.deliver_to_live_owner(tmp_path, foreign, "hello", delivery_id="b" * 32)
    with pytest.raises(ValueError, match="delivery id"):
        mailbox.deliver_to_live_owner(tmp_path, _owner(tmp_path), "hello", delivery_id="../evil")
    assert authority.calls == []


def test_receipt_read_forwards_the_immutable_author(tmp_path, monkeypatch):
    """F10: an authored admission is re-read with its stored author, so the authority sees the same payload."""
    authority = _FakeAuthority()
    monkeypatch.setattr(mailbox, "authority_delivery", authority)
    delivery_id = "d" * 32
    author = {"kind": "user", "id": "u-1", "display": "Ann"}
    queued = mailbox.deliver_to_live_owner(tmp_path, _owner(tmp_path), "hello", delivery_id=delivery_id, author=author)
    assert mailbox.read_delivery_result(tmp_path, delivery_id) == queued
    assert [params.get("author") for _home, params in authority.calls] == [author, author]


@pytest.mark.parametrize("terminal_status", ["settled", "failed", "cancelled"])
def test_terminal_receipt_is_immutable(tmp_path, terminal_status):
    delivery_id = "c" * 32
    with mailbox._locked(tmp_path) as root:
        mailbox._write(root / f"{delivery_id}.json", dict(delivery_id=delivery_id, status="claimed",
                                                          message="hello", created_at=1))
    receipt = mailbox.complete_delivery(tmp_path, delivery_id, status=terminal_status, reply="answer")
    assert mailbox.read_delivery_result(tmp_path, delivery_id) == receipt
    assert mailbox.complete_delivery(tmp_path, delivery_id, status=terminal_status, reply="réponse 世界") == receipt
    with pytest.raises(ValueError):
        mailbox.complete_delivery(tmp_path, delivery_id, status=terminal_status, reply="rewrite")
    with pytest.raises(ValueError):
        mailbox.complete_delivery(tmp_path, "d" * 32, status="not-terminal")

@pytest.mark.parametrize("intent_state", ["new", "existing", "raced"])
def test_live_dm_bom_readers_preserve_pinned_intent(tmp_path, monkeypatch, intent_state):
    from pathlib import Path

    from hermes_cli.active_sessions import try_acquire_active_session
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox, bot_mode_dm

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(
        session_id="chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata=dict(live_session_id="live", bot_live_delivery_consumer=True))
    assert refusal is None and lease is not None
    try:
        owner = mailbox.find_canonical_live_owner(tmp_path)
        assert owner is not None
        payload = tmp_path / "message.txt"
        payload.write_bytes("héllo 世界".encode("utf-8-sig"))
        intent_path = Path(str(payload) + ".live.json")
        author = {"id": "bot:coder", "name": "Renée", "is_bot": True}
        intent = dict(owner=owner, message="pinned 世界", delivery_id="d" * 32, author=author)
        encoded = json.dumps(intent, ensure_ascii=False).encode("utf-8-sig")
        if intent_state == "existing":
            intent_path.write_bytes(encoded)
        elif intent_state == "raced":
            real_open = os.open

            def competing_intent(path, flags, *args, **kwargs):
                if Path(path) == intent_path:
                    intent_path.write_bytes(encoded)
                return real_open(path, flags, *args, **kwargs)

            monkeypatch.setattr(os, "open", competing_intent)
        record = bot_mode_dm._admit_live_dm(tmp_path, str(payload), author)
        assert record is not None
        assert record["message"] == ("héllo 世界" if intent_state == "new" else "pinned 世界")
        assert record["owner"] == owner and record["author"] == author
        if intent_state == "new":
            assert not intent_path.read_bytes().startswith(b"\xef\xbb\xbf")
            intent_path.write_bytes(b"\xef\xbb\xbf" + intent_path.read_bytes())
        payload.write_text("must not replace pinned payload", encoding="utf-8")
        assert bot_mode_dm._admit_live_dm(None, str(payload)) == record
        claim = mailbox.claim_pending_delivery(tmp_path, owner)
        assert claim is not None and claim["delivery_id"] == record["delivery_id"]
        assert mailbox.claim_pending_delivery(tmp_path, owner) is None
        intent_path.write_bytes(b"\xef\xbb\xbf{broken")
        assert bot_mode_dm._run_delivery([], str(payload), stdin_file=False, profile_home=tmp_path) == 1
        assert payload.exists()  # Ambiguous admission must retain its evidence, not retry a transport.
    finally:
        lease.release()
        db.close()

def _ready(instance_id):
    from types import SimpleNamespace

    return lambda home, timeout: SimpleNamespace(state="ready", endpoint=SimpleNamespace(instance_id=instance_id))


def test_canonical_owner_is_the_authority_and_follows_compression(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    import hermes_cli.gateway_runtime as runtime

    from types import SimpleNamespace

    monkeypatch.setattr(runtime, "discover_gateway_endpoint",
                        lambda home, timeout: SimpleNamespace(state="absent", endpoint=None))
    with pytest.raises(ValueError, match="authority"):
        mailbox.find_canonical_live_owner(tmp_path)

    monkeypatch.setattr(runtime, "discover_gateway_endpoint", _ready("authority-1"))
    assert mailbox.find_canonical_live_owner(tmp_path) is None  # no state.db yet
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="scratch", source="cli")
        db.set_session_title("scratch", "Scratch")
        # No Bot Chat yet is still a deliverable owner: the authority creates the chat on
        # first delivery (create-if-missing), so discovery reports an empty tip, not None.
        assert mailbox.find_canonical_live_owner(tmp_path)["session_id"] == ""
        db.create_session(session_id="chat", source="cli")
        db.set_session_title("chat", "Bot Chat")
        owner = mailbox.find_canonical_live_owner(tmp_path)
        assert owner["canonical"] is True and owner["lease_id"] == "authority-1"
        assert owner["session_id"] == owner["live_session_id"] == "chat"
        db.end_session("chat", "compression")
        db.create_session(session_id="tip", source="cli", parent_session_id="chat")
        assert mailbox.find_canonical_live_owner(tmp_path)["session_id"] == "tip"
    finally:
        db.close()


def test_delivery_keeps_the_sender_and_refuses_a_different_one_under_the_same_id(tmp_path, monkeypatch):
    from tools import bot_live_delivery as mailbox

    authority = _FakeAuthority()
    monkeypatch.setattr(mailbox, "authority_delivery", authority)
    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat", lease_id="lease", live_session_id="live")
    author = {"id": "bot:coder", "name": "coder", "is_bot": True}
    queued = mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="b" * 32, author=author)
    assert authority.calls[-1][1]["author"] == author
    assert authority.records["b" * 32]["author"] == author
    assert mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="b" * 32, author=author) == queued
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(tmp_path, owner, "hello", delivery_id="b" * 32, author={**author, "id": "bot:other"})
    mailbox.deliver_to_live_owner(tmp_path, owner, "no sender", delivery_id="c" * 32)
    assert "author" not in authority.calls[-1][1]


def test_non_dict_ticket_fails_exact_id_reads_closed(tmp_path):
    """Malformed is not absent: an exact-id receipt read raises instead of reporting "no record"."""
    from tools import bot_live_delivery as mailbox

    bad = tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME / f"{'e' * 32}.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('"oops"', encoding="utf-8")  # parses, but is not a record
    with pytest.raises(ValueError):
        mailbox.read_delivery_result(tmp_path, "e" * 32)
    assert bad.read_text(encoding="utf-8") == '"oops"'


def test_existing_mailbox_lock_does_not_fsync_parent_dirs(tmp_path, monkeypatch):
    """The lock is re-entered on every receipt read; only a freshly created mailbox links its parents."""
    from tools import bot_live_delivery as mailbox

    calls = []
    monkeypatch.setattr(mailbox, "fsync_directory", lambda path: calls.append(path))
    with mailbox._locked(tmp_path):
        pass
    assert len(calls) == 2
    with mailbox._locked(tmp_path):
        pass
    assert len(calls) == 2
