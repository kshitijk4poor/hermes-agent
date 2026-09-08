"""Fresh local policy is frozen and scoped, not process launcher state."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_policy_selects_surface_and_isolates_cwd(tmp_path):
    from gateway.session_policy import build_policy, policy_scope
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.terminal_scope import terminal_env
    from hermes_state_runtime import RuntimeStoreError

    cfg = {'platform_toolsets': {'cli': ['terminal']}}
    before = dict(os.environ)
    policies = []
    for source in ('cli', 'tui', 'gui'):
        cwd = tmp_path / source
        cwd.mkdir()
        policies.append(build_policy({'source': source, 'cwd': str(cwd), 'model': source}, cfg))
    assert policies[2].platform == 'desktop'
    assert 'desktop_ui' in policies[2].toolsets
    assert all('desktop_ui' not in p.toolsets for p in policies[:2])
    cfg['platform_toolsets']['cli'].clear()
    assert 'terminal' in policies[0].toolsets

    def run(policy):
        with policy_scope(policy):
            assert str(resolve_agent_cwd()) == policy.cwd
            assert terminal_env('TERMINAL_CWD') == policy.cwd
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(run, policies))
    assert dict(os.environ) == before
    for params in ({'source': 'cron'}, {'toolsets': ['not-a-toolset']}, {'cwd': '.'},
                   {'provider': 'custom'}, {'skills': ['x']}, {'toolsets': ['desktop_ui'], 'source': 'cli'}):
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            build_policy(params, {})


def test_launch_policy_reaches_real_turn_runner(tmp_path):
    import json
    from pathlib import Path
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / 'home', tmp_path / 'state'
    home.mkdir()
    state.mkdir()
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo))
    result = subprocess.run([sys.executable, str(Path(__file__).parent / 'fixtures' / 'session_policy_peer.py')],
                            cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=130)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr
    receipt = json.loads((state / 'policy-receipt.json').read_text())
    assert receipt['cwd_effects'] and receipt['same_agents'] and receipt['no_spill']
    print(json.dumps(receipt))
