"""Current connector membership, not role flags, authorizes durable work."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

# The probe drives the real Discord adapter (a `messaging` extra); CI's base venv has no discord.py.
pytest.importorskip("discord")


def _probe(tmp_path, mode):
    tmp_path.mkdir(exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / 'home', tmp_path / 'state'
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo))
    result = subprocess.run([sys.executable, str(Path(__file__).parent / 'fixtures' /
                             'native_role_peer.py'), mode], cwd=repo, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)


def test_discord_role_membership_reaches_real_authority_and_model(tmp_path):
    _probe(tmp_path / 'positive', 'positive')
    _probe(tmp_path / 'multiplex', 'multiplex')


def test_role_preflight_fences_await_and_restart_without_consuming_work(tmp_path):
    _probe(tmp_path / 'fences', 'fences')
    for mode in ('capture', 'recover', 'recover-again'):
        _probe(tmp_path / 'restart', mode)
