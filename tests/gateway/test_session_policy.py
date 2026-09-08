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
