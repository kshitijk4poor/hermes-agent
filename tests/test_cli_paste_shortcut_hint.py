"""Tests for OS-specific CLI paste shortcut hints."""

import cli as cli_mod
from cli import HermesCLI


def _make_cli():
    return HermesCLI.__new__(HermesCLI)


def test_primary_paste_shortcut_is_cmd_v_on_macos(monkeypatch):
    cli = _make_cli()
    monkeypatch.setattr(cli_mod.sys, "platform", "darwin")

    assert cli._paste_shortcut_hint() == "Cmd+V"


def test_primary_paste_shortcut_is_ctrl_v_on_regular_linux(monkeypatch):
    cli = _make_cli()
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    monkeypatch.setattr("hermes_cli.clipboard._is_wsl", lambda: False)

    assert cli._paste_shortcut_hint() == "Ctrl+V"


def test_primary_paste_shortcut_is_alt_v_on_wsl(monkeypatch):
    cli = _make_cli()
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    monkeypatch.setattr("hermes_cli.clipboard._is_wsl", lambda: True)

    assert cli._paste_shortcut_hint() == "Alt+V"


def test_help_hint_keeps_alt_v_as_fallback_when_primary_differs(monkeypatch):
    cli = _make_cli()
    monkeypatch.setattr(cli_mod.sys, "platform", "darwin")

    assert cli._paste_image_help_hint() == "Cmd+V or Alt+V"


def test_help_hint_avoids_duplicate_alt_v_on_wsl(monkeypatch):
    cli = _make_cli()
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    monkeypatch.setattr("hermes_cli.clipboard._is_wsl", lambda: True)

    assert cli._paste_image_help_hint() == "Alt+V"
