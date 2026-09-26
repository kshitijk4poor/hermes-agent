"""An MSC3245 voice event carries an empty ``m.mentions`` block even when the user typed a mention
while recording: Element sends the typed mention as a separate ``m.text`` event right after. Under
``MATRIX_REQUIRE_MENTION=true`` the voice must be parked and claimed by that bare mention, scoped to
the same room and sender, and only within the claim window.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _make_adapter(monkeypatch):
    for name in ("MATRIX_ALLOWED_ROOMS", "MATRIX_FREE_RESPONSE_ROOMS", "MATRIX_HOME_ROOM",
                 "MATRIX_SESSION_SCOPE", "MATRIX_HOME_ROOM_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "true")
    monkeypatch.setenv("MATRIX_AUTO_THREAD", "false")
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(PlatformConfig(
        enabled=True, token="syt_test_token",
        extra={"homeserver": "https://matrix.example.org", "user_id": "@hermes:example.org"}))
    adapter._startup_ts = time.time() - 10
    adapter.handle_message = AsyncMock()
    adapter._client = None
    adapter._resolve_room_identity = AsyncMock(return_value=SimpleNamespace(
        display_name="Group Room", room_topic=None, server_name="example.org", chat_type="group"))
    adapter._is_dm_room = AsyncMock(return_value=False)
    adapter._download_and_cache_media = AsyncMock(return_value=None)
    adapter._text_batch_delay_seconds = 0.0  # make text dispatch observable without the debounce
    return adapter


def _voice_event(room_id="!a:example.org", event_id="$voice1"):
    return SimpleNamespace(
        sender="@alice:example.org", event_id=event_id, room_id=room_id,
        timestamp=int(time.time() * 1000),
        content={
            "body": "voice message", "msgtype": "m.audio", "url": "mxc://example.org/voice",
            "info": {"mimetype": "audio/ogg", "size": 2048},
            "org.matrix.msc3245.voice": {}, "m.mentions": {},
        })


def _bare_mention(room_id="!a:example.org", event_id="$text1"):
    return SimpleNamespace(
        sender="@alice:example.org", event_id=event_id, room_id=room_id,
        timestamp=int(time.time() * 1000),
        content={"body": "@hermes:example.org", "msgtype": "m.text",
                 "m.mentions": {"user_ids": ["@hermes:example.org"]}})


def _dispatched(adapter):
    return [(c.args[0].source.chat_id, c.args[0].message_id) for c in adapter.handle_message.await_args_list]


@pytest.mark.asyncio
async def test_bare_mention_claims_parked_voice(monkeypatch):
    """Only a bare mention in the voice's own room claims it; another room's mention is plain text."""
    adapter = _make_adapter(monkeypatch)

    await adapter._on_room_message(_voice_event())
    await adapter._on_room_message(_bare_mention(room_id="!b:example.org", event_id="$textB"))
    assert _dispatched(adapter) == [("!b:example.org", "$textB")]

    await adapter._on_room_message(_bare_mention(event_id="$textA"))
    assert _dispatched(adapter) == [("!b:example.org", "$textB"), ("!a:example.org", "$voice1")]


@pytest.mark.asyncio
async def test_expired_parked_voice_is_not_dispatched(monkeypatch):
    """Past the claim window the parked voice is dropped; the mention is handled as plain text."""
    import plugins.platforms.matrix.adapter as adapter_mod

    adapter = _make_adapter(monkeypatch)
    await adapter._on_room_message(_voice_event())
    monkeypatch.setattr(adapter_mod, "_VOICE_CLAIM_WINDOW_SECONDS", -1)

    await adapter._on_room_message(_bare_mention())

    assert _dispatched(adapter) == [("!a:example.org", "$text1")]
