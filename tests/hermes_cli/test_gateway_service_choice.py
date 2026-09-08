"""Service consent through real config/discovery/install; only OS peers are fake."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

from hermes_cli import config as config_api
from hermes_cli import gateway
from hermes_cli import gateway_setup_service as setup_service


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    # The installer intentionally rejects /tmp profiles. A disposable RAM-backed
    # home exercises that real guard without disabling it or touching user state.
    with tempfile.TemporaryDirectory(prefix="hermes-choice-", dir="/dev/shm") as directory:
        home = Path(directory)
        profile = home / "profile"
        profile.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(profile))
        runtime = home / "run"
        runtime.mkdir()
        (runtime / "bus").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        calls = tmp_path / "calls.jsonl"
        state = tmp_path / "active"
        for name in ("systemctl", "loginctl", "sudo"):
            exe = bin_dir / name
            exe.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "with calls.open('a') as stream: stream.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n"
                f"active = pathlib.Path({str(state)!r})\n"
                "args = sys.argv[1:]\n"
                "if 'enable' in args and os.getenv('CHOICE_FAIL'): sys.exit(1)\n"
                "if 'start' in args: active.touch()\n"
                "if 'is-active' in args: print('active' if active.exists() else 'inactive')\n"
                "elif 'show-user' in args: print('yes')\n"
                "elif 'is-system-running' in args: print('running')\n",
                encoding="utf-8",
            )
            exe.chmod(0o700)
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
        # Inject the owned executable at the OS command boundary, retaining the
        # live-system guard (which correctly blocks a command named systemctl).
        peer = bin_dir / "supervisor-peer"
        peer.write_bytes((bin_dir / "systemctl").read_bytes())
        peer.chmod(0o700)
        monkeypatch.setattr(gateway, "_systemctl_cmd", lambda system=False: [str(peer)])
        yield profile, calls


def _operations(calls):
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


@pytest.mark.linux_only
@pytest.mark.parametrize("choice", [None, "decline", "install"])
def test_import_and_ordinary_setup_never_authorize_install(supervisor, choice):
    profile, calls = supervisor
    config_api.save_config({"gateway": {"service_install_choice": choice}})
    before = (profile / "config.yaml").read_bytes()
    assert setup_service.ensure_gateway_service() is False
    assert setup_service.ensure_gateway_service(context="import") is False
    assert not gateway.get_systemd_unit_path().exists()
    assert not any(set(op) & {"enable", "enable-linger", "start", "daemon-reload"} for op in _operations(calls))
    assert (profile / "config.yaml").read_bytes() == before


@pytest.mark.linux_only
@pytest.mark.parametrize("answer, fail", [(False, False), (True, False), (True, True)])
def test_explicit_setup_consent_is_durable_only_after_success(supervisor, monkeypatch, answer, fail):
    profile, calls = supervisor
    config_api.save_config({"gateway": {"service_install_choice": None}, "custom": "keep"})
    prompts = []
    monkeypatch.setattr(gateway, "prompt_yes_no", lambda *args: prompts.append(args) or answer)
    stream = io.StringIO()
    stream.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", stream)
    if fail:
        monkeypatch.setenv("CHOICE_FAIL", "1")
    config = config_api.load_config()
    result = setup_service.ensure_gateway_service(interactive=True, config=config)
    expected = None if fail else ("install" if answer else "decline")
    assert config_api.load_config()["gateway"]["service_install_choice"] == expected
    assert config["gateway"]["service_install_choice"] == expected
    assert config_api.load_config()["custom"] == "keep"
    assert result is (answer and not fail)
    assert len(prompts) == 1
    if not fail:
        setup_service.ensure_gateway_service(interactive=True)
        assert len(prompts) == 1
    if not answer:
        assert not gateway.get_systemd_unit_path().exists()
        assert not any(set(op) & {"enable", "enable-linger", "start", "daemon-reload"} for op in _operations(calls))


@pytest.mark.linux_only
@pytest.mark.parametrize("start_now", [False])
def test_wizard_declined_service_does_not_install(supervisor, monkeypatch, start_now):
    profile, calls = supervisor
    config_api.save_config({"gateway": {"service_install_choice": None}})
    answers = iter([start_now, False])
    monkeypatch.setattr(gateway, "prompt_yes_no", lambda *args: next(answers))
    monkeypatch.setattr(gateway, "prompt_choice", lambda *args, **kwargs: 0)
    spawned = []
    # Popen is the unmanaged process boundary; no real gateway is launched.
    original_popen = gateway.subprocess.Popen

    def popen(argv, *args, **kwargs):
        if "hermes_cli.main" in argv:
            spawned.append((argv, kwargs))
            return None
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(gateway.subprocess, "Popen", popen)
    setup_service._wizard_install_service("systemd")
    assert not gateway.get_systemd_unit_path().exists()
    assert bool(spawned) is start_now
