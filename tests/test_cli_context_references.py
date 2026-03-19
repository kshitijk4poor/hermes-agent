from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import cli as cli_module
from cli import HermesCLI


def _make_cli_stub() -> HermesCLI:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = "anthropic/claude-opus-4.6"
    cli_obj.base_url = "https://openrouter.ai/api/v1"
    cli_obj.api_key = "test-key"
    cli_obj.console = MagicMock()
    cli_obj.console.width = 80
    cli_obj.show_reasoning = False
    cli_obj.bell_on_complete = False
    cli_obj._streaming_box_opened = False
    cli_obj._stream_started = False
    cli_obj._stream_buf = ""
    cli_obj._stream_box_opened = False
    cli_obj._secret_capture_callback = lambda *_args, **_kwargs: None
    cli_obj._ensure_runtime_credentials = lambda: True
    cli_obj._resolve_turn_agent_config = lambda _message: {
        "signature": "sig",
        "model": cli_obj.model,
        "runtime": {},
        "label": "default",
    }
    cli_obj._active_agent_route_signature = "sig"
    cli_obj._init_agent = lambda **_kwargs: True
    cli_obj._preprocess_images_with_vision = lambda message, _images: message
    cli_obj._reset_stream_state = lambda: None
    cli_obj._invalidate = lambda *args, **kwargs: None
    cli_obj._flush_stream = lambda: None
    cli_obj._clear_session_env = lambda: None
    cli_obj._interrupt_queue = None
    cli_obj._clarify_state = False
    cli_obj._clarify_freetext = False
    cli_obj._voice_tts = False
    cli_obj._voice_mode = False
    cli_obj._voice_continuous = False
    cli_obj._voice_recording = False
    cli_obj._voice_tts_done = MagicMock()
    cli_obj._session_db = None
    cli_obj.session_id = "sess-1"
    cli_obj.conversation_history = []
    cli_obj.agent = SimpleNamespace(
        run_conversation=lambda **kwargs: {
            "messages": [{"role": "user", "content": kwargs["user_message"]}],
            "final_response": "ok",
        },
        interrupt=lambda *_args, **_kwargs: None,
    )
    return cli_obj


def test_cli_expand_context_references_uses_model_context(monkeypatch, tmp_path):
    cli_obj = _make_cli_stub()
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *args, **kwargs: 4096,
    )

    captured = {}

    def fake_preprocess(message, *, cwd, context_length, url_fetcher=None):
        captured["message"] = message
        captured["cwd"] = cwd
        captured["context_length"] = context_length
        return SimpleNamespace(
            message="expanded message",
            original_message=message,
            warnings=["warn"],
            injected_tokens=123,
            references=[object()],
            expanded=True,
            blocked=False,
        )

    monkeypatch.setattr("agent.context_references.preprocess_context_references", fake_preprocess)

    result = cli_obj._expand_context_references_for_turn("inspect @file:notes.txt", cwd=tmp_path)

    assert result.message == "expanded message"
    assert captured["message"] == "inspect @file:notes.txt"
    assert captured["cwd"] == tmp_path
    assert captured["context_length"] == 4096


def test_cli_chat_sends_expanded_message_to_agent(monkeypatch, tmp_path):
    cli_obj = _make_cli_stub()
    monkeypatch.setattr(cli_module, "ChatConsole", lambda: MagicMock(print=lambda *_a, **_k: None))
    monkeypatch.setattr(cli_module, "_accent_hex", lambda: "gold")
    monkeypatch.setattr(
        "agent.title_generator.maybe_auto_title",
        lambda *_args, **_kwargs: None,
    )

    cli_obj._expand_context_references_for_turn = lambda message, **_kwargs: SimpleNamespace(
        message="expanded message",
        original_message=message,
        warnings=[],
        injected_tokens=12,
        references=[object()],
        expanded=True,
        blocked=False,
    )

    response = cli_obj.chat("inspect @file:notes.txt")

    assert response == "ok"
    assert cli_obj.conversation_history[0]["content"] == "expanded message"
