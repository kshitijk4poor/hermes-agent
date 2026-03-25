"""Tests for ACP OpenAI-shim helpers."""

import os

import pytest

from agent.external_acp_client import (
    _finalize_acp_usage,
    _usage_from_acp_prompt_result,
)


def test_usage_from_nested_usage_dict():
    out = _usage_from_acp_prompt_result(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
    )
    assert out == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


def test_usage_from_input_output_aliases():
    out = _usage_from_acp_prompt_result({"input_tokens": 5, "output_tokens": 7})
    assert out == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}


def test_usage_missing_returns_none():
    assert _usage_from_acp_prompt_result({}) is None
    assert _usage_from_acp_prompt_result(None) is None


def test_finalize_acp_usage_prefers_real_counts(monkeypatch):
    monkeypatch.delenv("HERMES_ACP_ESTIMATE_USAGE", raising=False)
    out = _finalize_acp_usage(
        "x" * 400,
        "y" * 400,
        "",
        {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
    )
    assert out == {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}


def test_finalize_acp_usage_heuristic_when_enabled(monkeypatch):
    monkeypatch.setenv("HERMES_ACP_ESTIMATE_USAGE", "1")
    out = _finalize_acp_usage("abcd" * 10, "xy" * 6, "zz", None)
    assert out == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}


def test_finalize_acp_usage_heuristic_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("HERMES_ACP_ESTIMATE_USAGE", raising=False)
    out = _finalize_acp_usage("abcd" * 10, "xy" * 6, "", None)
    assert out == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}


def test_finalize_acp_usage_no_heuristic_when_disabled(monkeypatch):
    monkeypatch.setenv("HERMES_ACP_ESTIMATE_USAGE", "0")
    assert _finalize_acp_usage("abcd" * 100, "x", "", None) is None


@pytest.mark.parametrize("kind", ["agent_message_chunk", "agent_thought_chunk"])
def test_handle_server_message_invokes_delta_callbacks(kind):
    from agent.external_acp_client import ExternalACPClient

    c = ExternalACPClient(
        provider_id="t",
        provider_name="T",
        acp_command="/bin/true",
        acp_args=[],
    )
    text_deltas: list[str] = []
    thought_deltas: list[str] = []
    msg = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": kind,
                "content": {"text": "hi"},
            }
        },
    }
    text_parts: list[str] = []
    reasoning_parts: list[str] = []

    class _DummyProc:
        stdin = None

    c._handle_server_message(
        msg,
        process=_DummyProc(),  # type: ignore[arg-type]
        cwd=os.getcwd(),
        text_parts=text_parts,
        reasoning_parts=reasoning_parts,
        on_text_delta=(lambda t: text_deltas.append(t)) if kind == "agent_message_chunk" else None,
        on_reasoning_delta=(lambda t: thought_deltas.append(t)) if kind == "agent_thought_chunk" else None,
    )
    if kind == "agent_message_chunk":
        assert text_parts == ["hi"]
        assert text_deltas == ["hi"]
        assert reasoning_parts == []
    else:
        assert reasoning_parts == ["hi"]
        assert thought_deltas == ["hi"]
        assert text_parts == []
