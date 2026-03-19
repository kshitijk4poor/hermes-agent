from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.TELEGRAM: MagicMock(send=AsyncMock())}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._clear_session_env = lambda: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_gateway_handle_message_expands_context_references(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    (tmp_path / "notes.txt").write_text("hello from gateway\n", encoding="utf-8")

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 10,
            "input_tokens": 20,
            "output_tokens": 5,
            "model": "anthropic/test",
        }
    )

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "anthropic/claude-opus-4.6")
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )
    monkeypatch.setenv("MESSAGING_CWD", str(tmp_path))

    result = await runner._handle_message(_make_event("inspect @file:notes.txt"))

    assert result == "ok"
    sent_message = runner._run_agent.await_args.kwargs["message"]
    assert "hello from gateway" in sent_message
    assert "@file:notes.txt" in sent_message


@pytest.mark.asyncio
async def test_gateway_preprocess_context_references_returns_warning_when_blocked(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 20,
    )
    (tmp_path / "notes.txt").write_text("x" * 400, encoding="utf-8")

    result = await GatewayRunner._expand_context_references_for_message(
        runner,
        "inspect @file:notes.txt",
        cwd=tmp_path,
        model="anthropic/claude-opus-4.6",
        base_url="",
        api_key="",
    )

    assert result.blocked
    assert any("50%" in warning for warning in result.warnings)
