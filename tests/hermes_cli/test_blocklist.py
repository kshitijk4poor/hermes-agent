"""Tests for the hermes blocklist CLI command."""

from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

from hermes_cli.config import load_config


def test_add_blocklist_host_updates_config(capsys):
    from hermes_cli.blocklist import blocklist_command

    blocklist_command(Namespace(blocklist_action="add", host="Example.COM", source=None, list_id=None, url=None))

    cfg = load_config()
    assert cfg["web_policy"]["local_blocks"] == ["example.com"]
    assert "example.com" in capsys.readouterr().out


def test_subscribe_adds_deduplicated_subscription(capsys):
    from hermes_cli.blocklist import blocklist_command

    called = []
    args = Namespace(
        blocklist_action="subscribe",
        host=None,
        source="https://lists.example/index.yaml",
        list_id="malware",
        url=None,
    )
    from hermes_cli import blocklist as blocklist_mod
    blocklist_mod.refresh_subscriptions = lambda: called.append("refresh") or {
        "success": True,
        "subscriptions": [],
        "rule_count": 0,
    }
    blocklist_command(args)
    blocklist_command(args)

    cfg = load_config()
    assert cfg["web_policy"]["subscriptions"] == [
        {"source": "https://lists.example/index.yaml", "list_id": "malware"}
    ]
    assert called == ["refresh", "refresh"]
    assert "malware" in capsys.readouterr().out


def test_subscribe_reports_refresh_failure(capsys):
    from hermes_cli.blocklist import blocklist_command

    args = Namespace(
        blocklist_action="subscribe",
        host=None,
        source="https://lists.example/index.yaml",
        list_id="malware",
        url=None,
    )
    from hermes_cli import blocklist as blocklist_mod
    blocklist_mod.refresh_subscriptions = lambda: {
        "success": True,
        "subscriptions": [
            {
                "source": "https://lists.example/index.yaml",
                "list_id": "malware",
                "error": "network down",
            }
        ],
        "rule_count": 0,
    }

    blocklist_command(args)

    assert "refresh failed" in capsys.readouterr().out.lower()


def test_remove_reports_not_found(capsys):
    from hermes_cli.blocklist import blocklist_command

    blocklist_command(Namespace(blocklist_action="remove", host="missing.example", source=None, list_id=None, url=None))

    assert "not found" in capsys.readouterr().out.lower()


def test_unsubscribe_reports_not_found(capsys):
    from hermes_cli.blocklist import blocklist_command

    blocklist_command(
        Namespace(
            blocklist_action="unsubscribe",
            host=None,
            source="https://lists.example/index.yaml",
            list_id="malware",
            url=None,
        )
    )

    assert "not found" in capsys.readouterr().out.lower()


def test_why_uses_explain_url(capsys, monkeypatch):
    from hermes_cli.blocklist import blocklist_command
    from tools.url_policy import PolicyDecision

    monkeypatch.setattr(
        "hermes_cli.blocklist.explain_url",
        lambda url: PolicyDecision(
            allowed=False,
            requested_url=url,
            normalized_url=url,
            host="evil.example",
            final_url=None,
            decision_source="shared_list",
            reason="blocked by shared_list",
            rule_identity="remote|evil.example",
            source_identity="remote#malware",
            source_title="Malware",
        ),
    )

    blocklist_command(
        Namespace(
            blocklist_action="why",
            host=None,
            source=None,
            list_id=None,
            url="https://evil.example",
        )
    )

    out = capsys.readouterr().out
    assert "shared_list" in out
    assert "remote|evil.example" in out


def test_main_routes_blocklist_command():
    import hermes_cli.main as main_mod

    with (
        patch("hermes_cli.blocklist.blocklist_command") as mock_blocklist,
        patch("sys.argv", ["hermes", "blocklist", "add", "Example.COM"]),
    ):
        main_mod.main()

    mock_blocklist.assert_called_once()
    args = mock_blocklist.call_args[0][0]
    assert args.command == "blocklist"
    assert args.blocklist_action == "add"
    assert args.host == "Example.COM"
