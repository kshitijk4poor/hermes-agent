"""`hermes profile create` through a live gateway process (real ``gateway.run``).

* local: the CLI on the gateway's own home has the profile made by the gateway (this process's
  ``create_profile`` is made to fail, so only the gateway can have made it), and a refused request (an
  existing name) comes back as the CLI's usual error;
* named: a CLI whose own home has no gateway, pointed at one by ``HERMES_TUI_GATEWAY_URL`` (loopback),
  has the profile made in the GATEWAY's home and nothing in its own;
* an unreachable URL is an error, never a silent local create;
* no gateway and no URL: the CLI creates locally, as it always has.
"""
from __future__ import annotations

from argparse import Namespace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.gateway.test_native_http_auth import daemon

ROOT = Path(__file__).resolve().parents[2]


def _create(home: Path, user: Path, *argv, env=None):
    base = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    base.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(ROOT),
                PYTHONUNBUFFERED="1", **(env or {}))
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "profile", "create", *argv, "--no-alias"],
                          cwd=ROOT, env=base, capture_output=True, text=True, timeout=240, check=False)


def _cli_args(name):
    return Namespace(profile_name=name, clone=False, clone_all=False, no_alias=True, no_skills=False,
                     clone_from=None, clone_channels=False, sync_imports=False, description=None)


@pytest.mark.platforms("linux", "macos")
def test_cli_creates_through_a_live_gateway_local_and_remote(tmp_path, monkeypatch, capsys):
    with daemon(tmp_path) as (home, descriptor):
        user = tmp_path / "user"

        # Local: this process is on the gateway's home and cannot create a profile itself.
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HOME", str(user))
        monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
        from hermes_cli import profile_cmd, profiles

        def refuse(*a, **k):
            raise AssertionError("created in the CLI process, not by the gateway")
        monkeypatch.setattr(profiles, "create_profile", refuse)
        profile_cmd._profile_create(_cli_args("viagw"))
        out = capsys.readouterr().out
        assert "Profile 'viagw' created at" in out
        assert (home / "profiles" / "viagw" / "config.yaml").is_file()

        with pytest.raises(SystemExit) as refused:
            profile_cmd._profile_create(_cli_args("viagw"))
        assert refused.value.code == 1
        assert "A profile named 'viagw' already exists" in capsys.readouterr().out

        # Named: a different home with no gateway, pointed at this one by URL.
        terminal_home = tmp_path / "terminal-home"
        terminal_home.mkdir()
        url = descriptor["api_origin"].replace("http:", "ws:") + "/api/ws?token=normal-http-owner"
        remote = _create(terminal_home, user, "remote1", env={"HERMES_TUI_GATEWAY_URL": url})
        out = remote.stdout + remote.stderr
        assert remote.returncode == 0, out
        assert "normal-http-owner" not in out  # the credential in the URL is never printed
        assert (home / "profiles" / "remote1" / "config.yaml").is_file(), out
        assert not (terminal_home / "profiles").exists(), out

        # Unreachable URL: an error, and still nothing local.
        dead = _create(terminal_home, user, "remote2", env={"HERMES_TUI_GATEWAY_URL": "ws://127.0.0.1:9/api/ws"})
        assert dead.returncode == 1
        assert "could not reach the gateway at ws://127.0.0.1:9/api/ws" in dead.stdout + dead.stderr
        assert not (terminal_home / "profiles").exists()
        assert not (home / "profiles" / "remote2").exists()


def test_no_gateway_creates_locally(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    from hermes_cli import profile_cmd, profiles
    monkeypatch.setattr(profiles, "seed_profile_skills", lambda path, quiet=True: {"copied": []})
    profile_cmd._profile_create(_cli_args("solo"))
    assert "Profile 'solo' created at" in capsys.readouterr().out
    assert (home / "profiles" / "solo").is_dir()
