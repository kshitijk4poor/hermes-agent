"""Authority event contracts, separate from the transport's queue unit tests."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.linux_only
def test_full_observer_cannot_block_real_authority_execution(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / 'home', tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state),
               PYTHONPATH=str(repo), PYTHONUNBUFFERED='1')
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / 'fixtures' / 'authority_events_peer.py')],
        cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    print(result.stdout)
