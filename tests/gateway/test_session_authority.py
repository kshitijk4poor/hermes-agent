"""Real messaging ingress and authenticated WS must share one warm agent."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_messaging_then_authenticated_ws_reuses_live_agent(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    # A fresh interpreter keeps import-time profile caches and background workers
    # away from both the user's state and pytest's process-global fixtures.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "TZ") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state),
               PYTHONPATH=str(repo), PYTHONUNBUFFERED="1")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures" / "shared_authority_peer.py")],
        cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=100,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    receipt = json.loads((state / "receipt.json").read_text())
    print(json.dumps(receipt, indent=2))
    assert receipt["messaging_completed"], receipt
    assert receipt["unauthenticated_rejected"], receipt
    assert receipt["ws_completed"], receipt
    assert receipt["same_agent"], receipt
