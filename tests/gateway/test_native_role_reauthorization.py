"""Current connector membership, not role flags, authorizes durable work."""
import os
from pathlib import Path
import subprocess
import sys


def test_discord_role_membership_reaches_real_authority_and_model(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / 'home', tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'LANG', 'TZ') if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo))
    result = subprocess.run([sys.executable, str(Path(__file__).parent / 'fixtures' /
                             'native_role_peer.py')], cwd=repo, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
