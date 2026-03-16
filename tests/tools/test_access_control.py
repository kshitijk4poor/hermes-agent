"""Tests for scoped connected-account access control."""

import json

from model_tools import handle_function_call
from tools.access_control import evaluate_access, infer_mcp_operation
from tools.registry import registry


def test_evaluate_access_uses_read_only_platform_profile_for_writes():
    decision = evaluate_access(
        "ha_call_service",
        {"service": "homeassistant", "account": "homeassistant", "operation": "write"},
        platform="cron",
        config={
            "access_control": {
                "enabled": True,
                "default_scope": "full",
                "platform_profiles": {"cron": "read-only"},
            }
        },
    )

    assert decision.allowed is False
    assert decision.scope == "read-only"
    assert decision.platform == "cron"


def test_evaluate_access_allows_account_override_for_write():
    decision = evaluate_access(
        "mcp_github_create_issue",
        {"service": "mcp", "account": "mcp.github", "operation": "write"},
        platform="cron",
        config={
            "access_control": {
                "enabled": True,
                "default_scope": "full",
                "platform_profiles": {"cron": "read-only"},
                "accounts": {"mcp.github": {"scope": "full"}},
            }
        },
    )

    assert decision.allowed is True
    assert decision.scope == "full"


def test_infer_mcp_operation_fails_closed_for_mutating_verbs():
    assert infer_mcp_operation("mcp_github_create_issue") == "write"
    assert infer_mcp_operation("mcp_docs_read_page") == "read"
    assert infer_mcp_operation("mcp_unknown_sync") == "write"


def test_registry_hides_static_tool_when_scope_denies_it(monkeypatch):
    tool_name = "test_scope_hidden_tool"
    original = registry._tools.copy()
    try:
        registry.register(
            name=tool_name,
            toolset="testing",
            schema={
                "name": tool_name,
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kwargs: json.dumps({"ok": True}),
            access_fn=lambda args, **kwargs: {
                "service": "homeassistant",
                "account": "homeassistant",
                "operation": "write",
            },
            access_static=True,
        )
        monkeypatch.setattr(
            "tools.access_control._get_access_control_config",
            lambda config=None: {
                "enabled": True,
                "default_scope": "full",
                "platform_profiles": {"cron": "read-only", "cli": "full"},
            },
        )

        hidden = registry.get_definitions({tool_name}, quiet=True, platform="cron")
        shown = registry.get_definitions({tool_name}, quiet=True, platform="cli")

        assert hidden == []
        assert shown[0]["function"]["name"] == tool_name
    finally:
        registry._tools = original


def test_handle_function_call_returns_scope_violation_without_running_handler(monkeypatch):
    tool_name = "test_scope_blocked_tool"
    original = registry._tools.copy()
    called = {"value": False}
    try:
        registry.register(
            name=tool_name,
            toolset="testing",
            schema={
                "name": tool_name,
                "description": "blocked tool",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kwargs: called.__setitem__("value", True) or json.dumps({"ok": True}),
            access_fn=lambda args, **kwargs: {
                "service": "mcp",
                "account": "mcp.github",
                "operation": "write",
            },
            access_static=True,
        )
        monkeypatch.setattr(
            "tools.access_control._get_access_control_config",
            lambda config=None: {
                "enabled": True,
                "default_scope": "full",
                "platform_profiles": {"cron": "read-only"},
            },
        )

        result = json.loads(handle_function_call(tool_name, {}, platform="cron"))

        assert result["error_type"] == "scope_violation"
        assert called["value"] is False
    finally:
        registry._tools = original


def test_send_message_access_denied_on_read_only_platform(monkeypatch):
    """send_message access_fn derives operation from target arg; deny on read-only platform."""
    from tools.send_message_tool import _send_message_access

    monkeypatch.setattr(
        "tools.access_control._get_access_control_config",
        lambda config=None: {
            "enabled": True,
            "default_scope": "full",
            "platform_profiles": {"telegram": "read-only"},
        },
    )

    decision = evaluate_access(
        "send_message",
        _send_message_access({"target": "telegram:12345"}),
        platform="telegram",
    )

    assert decision.allowed is False
    assert decision.service == "messaging"
    assert decision.operation == "write"
    assert decision.scope == "read-only"


def test_send_message_access_allowed_on_full_platform(monkeypatch):
    """send_message should be allowed when platform has full scope."""
    from tools.send_message_tool import _send_message_access

    monkeypatch.setattr(
        "tools.access_control._get_access_control_config",
        lambda config=None: {
            "enabled": True,
            "default_scope": "full",
            "platform_profiles": {"cli": "full"},
        },
    )

    decision = evaluate_access(
        "send_message",
        _send_message_access({"target": "cli"}),
        platform="cli",
    )

    assert decision.allowed is True


def test_send_message_not_hidden_from_schema_without_access_static(monkeypatch):
    """send_message has access_fn but not access_static, so it stays visible in schemas."""
    from tools.send_message_tool import _send_message_access

    # Register a temp tool mimicking send_message (access_fn without access_static)
    tool_name = "test_send_msg_dynamic"
    original = registry._tools.copy()
    try:
        registry.register(
            name=tool_name,
            toolset="testing",
            schema={
                "name": tool_name,
                "description": "dynamic access tool",
                "parameters": {"type": "object", "properties": {}},
            },
            handler=lambda args, **kwargs: json.dumps({"ok": True}),
            access_fn=_send_message_access,
            # access_static=False (default) — schema check skipped
        )
        monkeypatch.setattr(
            "tools.access_control._get_access_control_config",
            lambda config=None: {
                "enabled": True,
                "default_scope": "full",
                "platform_profiles": {"telegram": "read-only"},
            },
        )

        # Should appear in schema even on read-only platform (no access_static)
        shown = registry.get_definitions({tool_name}, quiet=True, platform="telegram")
        assert len(shown) == 1
        assert shown[0]["function"]["name"] == tool_name
    finally:
        registry._tools = original


def test_sandbox_tool_calls_inherit_platform_access(monkeypatch):
    """Tools dispatched via execute_code sandbox should receive the originating platform."""
    from tools.access_control import evaluate_access, _get_access_control_config

    monkeypatch.setattr(
        "tools.access_control._get_access_control_config",
        lambda config=None: {
            "enabled": True,
            "default_scope": "full",
            "platform_profiles": {"cron": "read-only"},
        },
    )

    # Simulate what the sandbox RPC loop does: evaluate with the platform string
    decision = evaluate_access(
        "ha_call_service",
        {"service": "homeassistant", "account": "homeassistant", "operation": "write"},
        platform="cron",
    )

    assert decision.allowed is False
    assert decision.platform == "cron"

    # But same call from CLI should pass
    decision_cli = evaluate_access(
        "ha_call_service",
        {"service": "homeassistant", "account": "homeassistant", "operation": "write"},
        platform="cli",
    )
    assert decision_cli.allowed is True


def test_access_control_disabled_allows_everything(monkeypatch):
    """When enabled=False, all tools should be allowed regardless of scope."""
    monkeypatch.setattr(
        "tools.access_control._get_access_control_config",
        lambda config=None: {
            "enabled": False,
            "default_scope": "read-only",
            "platform_profiles": {},
        },
    )

    decision = evaluate_access(
        "ha_call_service",
        {"service": "homeassistant", "account": "homeassistant", "operation": "write"},
        platform="telegram",
    )

    assert decision.allowed is True
    assert "disabled" in decision.reason.lower()


def test_service_level_scope_override(monkeypatch):
    """Service-level scope should override platform profile."""
    monkeypatch.setattr(
        "tools.access_control._get_access_control_config",
        lambda config=None: {
            "enabled": True,
            "default_scope": "full",
            "platform_profiles": {"telegram": "read-only"},
            "services": {
                "homeassistant": {"scope": "full"},
            },
        },
    )

    decision = evaluate_access(
        "ha_call_service",
        {"service": "homeassistant", "account": "homeassistant", "operation": "write"},
        platform="telegram",
    )

    assert decision.allowed is True
    assert decision.scope == "full"
