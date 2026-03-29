"""Tests for tools/delta_bootstrap.py."""

from pathlib import Path

from tools import delta_bootstrap


def test_resolve_delta_command_prefers_override(monkeypatch):
    monkeypatch.setenv("HERMES_DELTA_COMMAND", "custom-delta --paging=always")
    monkeypatch.setattr(delta_bootstrap, "_resolved_command", None)

    resolved = delta_bootstrap.resolve_delta_command()

    assert resolved == ["custom-delta", "--paging=always"]


def test_resolve_delta_command_prefers_managed_binary(tmp_path, monkeypatch):
    managed = tmp_path / "delta"
    managed.write_text("stub")
    managed.chmod(0o755)

    monkeypatch.delenv("HERMES_DELTA_COMMAND", raising=False)
    monkeypatch.setattr(delta_bootstrap, "_resolved_command", None)
    monkeypatch.setattr(delta_bootstrap, "_managed_delta_path", lambda: managed)
    monkeypatch.setattr(delta_bootstrap.shutil, "which", lambda _name: None)

    resolved = delta_bootstrap.resolve_delta_command()

    assert resolved == [str(managed), "--paging=always"]


def test_resolve_delta_command_uses_path_binary(monkeypatch, tmp_path):
    found = str(tmp_path / "delta")

    monkeypatch.delenv("HERMES_DELTA_COMMAND", raising=False)
    monkeypatch.setattr(delta_bootstrap, "_resolved_command", None)
    monkeypatch.setattr(delta_bootstrap, "_managed_delta_path", lambda: Path(tmp_path / "managed-delta"))
    monkeypatch.setattr(delta_bootstrap.shutil, "which", lambda name: found if name == "delta" else None)

    resolved = delta_bootstrap.resolve_delta_command()

    assert resolved == [found, "--paging=always"]


def test_resolve_delta_command_bootstraps_when_missing(monkeypatch, tmp_path):
    installed = [str(tmp_path / "delta"), "--paging=always"]

    monkeypatch.delenv("HERMES_DELTA_COMMAND", raising=False)
    monkeypatch.setattr(delta_bootstrap, "_resolved_command", None)
    monkeypatch.setattr(delta_bootstrap, "_managed_delta_path", lambda: Path(tmp_path / "managed-delta"))
    monkeypatch.setattr(delta_bootstrap.shutil, "which", lambda _name: None)
    monkeypatch.setattr(delta_bootstrap, "_install_delta", lambda: installed)

    resolved = delta_bootstrap.resolve_delta_command()

    assert resolved == installed


def test_resolve_delta_command_returns_none_after_failed_bootstrap(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_DELTA_COMMAND", raising=False)
    monkeypatch.setattr(delta_bootstrap, "_resolved_command", None)
    monkeypatch.setattr(delta_bootstrap, "_managed_delta_path", lambda: Path(tmp_path / "managed-delta"))
    monkeypatch.setattr(delta_bootstrap.shutil, "which", lambda _name: None)
    monkeypatch.setattr(delta_bootstrap, "_install_delta", lambda: None)

    first = delta_bootstrap.resolve_delta_command()
    second = delta_bootstrap.resolve_delta_command()

    assert first is None
    assert second is None


def test_detect_asset_suffix_supported_targets(monkeypatch):
    monkeypatch.setattr(delta_bootstrap.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(delta_bootstrap.platform, "machine", lambda: "arm64")
    assert delta_bootstrap._detect_asset_suffix() == "aarch64-apple-darwin.tar.gz"

    monkeypatch.setattr(delta_bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(delta_bootstrap.platform, "machine", lambda: "x86_64")
    assert delta_bootstrap._detect_asset_suffix() == "x86_64-unknown-linux-gnu.tar.gz"

    monkeypatch.setattr(delta_bootstrap.platform, "system", lambda: "Windows")
    monkeypatch.setattr(delta_bootstrap.platform, "machine", lambda: "AMD64")
    assert delta_bootstrap._detect_asset_suffix() == "x86_64-pc-windows-msvc.zip"


def test_detect_asset_suffix_unsupported_target(monkeypatch):
    monkeypatch.setattr(delta_bootstrap.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(delta_bootstrap.platform, "machine", lambda: "x86_64")

    assert delta_bootstrap._detect_asset_suffix() is None
