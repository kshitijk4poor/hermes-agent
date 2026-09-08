"""Frozen, explicit local-client launch policy for the existing TurnRunner."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

CREATE_FIELDS = frozenset({'request_id', 'source', 'cwd', 'model', 'toolsets'})
SURFACES = {'cli': 'cli', 'tui': 'tui', 'gui': 'desktop'}


@dataclass(frozen=True)
class LocalSessionPolicy:
    source: str
    platform: str
    cwd: str
    model: str | None
    toolsets: tuple[str, ...]
    config_json: str
    request_json: str
    terminal_json: str

    def config(self):
        return json.loads(self.config_json)


def build_policy(params, config):
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import validate_toolset
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.terminal_scope import build_profile_terminal_scope
    from hermes_constants import get_hermes_home

    source = params.get('source', 'cli')
    if set(params) - CREATE_FIELDS or not isinstance(source, str) or source not in SURFACES:
        raise RuntimeStoreError('invalid_params')
    model = params.get('model')
    if 'model' in params and (not isinstance(model, str) or not model.strip()):
        raise RuntimeStoreError('invalid_params')
    cwd = params.get('cwd', str(resolve_agent_cwd()))
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise RuntimeStoreError('invalid_params')
    config = json.loads(json.dumps(config))
    explicit = params.get('toolsets')
    if 'toolsets' in params:
        if (not isinstance(explicit, list) or any(not isinstance(x, str) or not validate_toolset(x) for x in explicit)
                or ('desktop_ui' in explicit and source != 'gui')):
            raise RuntimeStoreError('invalid_params')
        config.setdefault('platform_toolsets', {})['cli'] = explicit
    enabled = _get_platform_tools(config, 'cli')
    if explicit is not None:
        from toolsets import resolve_toolset
        requested = {t for name in explicit for t in resolve_toolset(name)}
        effective = {t for name in enabled for t in resolve_toolset(name)}
        if not requested <= effective:
            raise RuntimeStoreError('invalid_params')
    elif source != 'cli':
        # Same surface toolsets as the native TUI factory, without its env inference.
        enabled.add('project')
        if source == 'gui':
            enabled.add('desktop_ui')
    terminal = build_profile_terminal_scope(get_hermes_home())
    terminal['TERMINAL_CWD'] = cwd
    request = {k: v for k, v in params.items() if k != 'request_id'}
    request.setdefault('source', 'cli')
    return LocalSessionPolicy(source, SURFACES[source], cwd, model, tuple(sorted(enabled)),
                              json.dumps(config), json.dumps(request, sort_keys=True), json.dumps(terminal))


def policy_for_source(runner, source):
    from gateway.session_local import LocalSessionAdapter
    from gateway.config import Platform
    if source.platform != Platform.LOCAL:
        return None
    adapter = runner._adapter_for_source(source)
    if isinstance(adapter, LocalSessionAdapter) and adapter.authorize_source(source):
        return adapter.policies.get(source.chat_id)
    return None


@contextmanager
def policy_scope(policy):
    if policy is None:
        yield
        return
    from agent.runtime_cwd import set_session_cwd
    from tools.terminal_scope import set_terminal_scope, reset_terminal_scope
    cwd_token = set_session_cwd(policy.cwd)
    terminal_token = set_terminal_scope(json.loads(policy.terminal_json))
    try:
        yield
    finally:
        reset_terminal_scope(terminal_token)
        cwd_token.var.reset(cwd_token)
