"""Local automation belongs to the owner, never an attached client."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('source', ['cli', 'tui', 'gui'])
def test_local_completion_joins_fifo_without_an_observer(tmp_path, source):
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env['PYTHONPATH'] = str(root)
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/local_automation_peer.py'),
        str(tmp_path), source], cwd=root, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
