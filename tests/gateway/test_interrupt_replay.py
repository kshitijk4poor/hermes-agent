"""Regression tests for replaying queued gateway events after interrupts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text: str, *, media_urls=None, media_types=None):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="user-1",
            user_name="tester",
        ),
        message_id="msg-1",
        media_urls=list(media_urls or []),
        media_types=list(media_types or []),
    )


class _SessionStoreStub:
    def __init__(self):
        self.entry = SimpleNamespace(
            session_key="agent:main:telegram:dm",
            session_id="sess-1",
            created_at=1,
            updated_at=1,
            was_auto_reset=False,
            last_prompt_tokens=0,
        )
        self.transcript = []

    def get_or_create_session(self, _source):
        return self.entry

    def load_transcript(self, _session_id):
        return list(self.transcript)

    def has_any_sessions(self):
        return bool(self.transcript)

    def append_to_transcript(self, _session_id, entry, skip_db=False):
        self.transcript.append(entry)

    def update_session(self, _session_key, **kwargs):
        for key, value in kwargs.items():
            setattr(self.entry, key, value)


def _make_runner(monkeypatch):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = {}
    runner.adapters = {}
    runner.session_store = _SessionStoreStub()
    runner._session_db = None
    runner._running_agents = {}
    runner._pending_approvals = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = False
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._set_session_env = MagicMock()
    runner._clear_session_env = MagicMock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._should_send_voice_reply = MagicMock(return_value=False)
    runner._send_voice_reply = AsyncMock()
    runner._run_process_watcher = AsyncMock()
    monkeypatch.setattr(gateway_run, "build_session_context", lambda source, config, session_entry: {})
    monkeypatch.setattr(gateway_run, "build_session_context_prompt", lambda context: "")
    return runner


@pytest.mark.asyncio
async def test_handle_message_replays_pending_command_event_after_interrupt(monkeypatch):
    runner = _make_runner(monkeypatch)
    initial_event = _make_event("keep working")
    pending_event = _make_event("/status")

    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "",
            "messages": [{"role": "assistant", "content": "partial"}],
            "history_offset": 0,
            "tools": [],
            "_pending_event": pending_event,
            "_resume_history": [{"role": "assistant", "content": "partial"}],
        }
    )
    runner._handle_status_command = AsyncMock(return_value="status ok")

    result = await runner._handle_message(initial_event)

    assert result == "status ok"
    runner._handle_status_command.assert_awaited_once()
    runner._run_agent.assert_awaited_once()

