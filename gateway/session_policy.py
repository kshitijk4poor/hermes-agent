"""Frozen, explicit local-client launch policy for the existing TurnRunner."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError

CREATE_FIELDS = frozenset({'request_id', 'source', 'cwd', 'model', 'toolsets',
                           'provider', 'base_url', 'reasoning', 'max_turns', 'ignore_rules', 'api_key'})
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
    credential_ref: str | None = None

    def config(self):
        return json.loads(self.config_json)

    @property
    def provider(self):
        return self.config().get('model', {}).get('provider')

    @property
    def base_url(self):
        return self.config().get('model', {}).get('base_url')

    @property
    def ignore_rules(self):
        return json.loads(self.request_json).get('ignore_rules', False)

    @property
    def max_turns(self):
        from hermes_cli.config import resolve_turn_limit
        cfg = self.config()
        return resolve_turn_limit(cfg.get('agent', {}).get('max_turns', cfg.get('max_turns')))

    @property
    def reasoning_config(self):
        from hermes_constants import resolve_reasoning_config
        return resolve_reasoning_config(self.config(), self.model or '')


def build_policy(params, config):
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import validate_toolset
    from agent.runtime_cwd import resolve_agent_cwd
    from tools.terminal_scope import build_profile_terminal_scope
    from hermes_constants import get_hermes_home

    source = params.get('source', 'cli')
    if set(params) - CREATE_FIELDS or not isinstance(source, str) or source not in SURFACES:
        raise RuntimeStoreError('invalid_params')
    if 'api_key' in params and (not isinstance(params['api_key'], str) or not params['api_key'].strip()):
        raise RuntimeStoreError('invalid_params')
    model = params.get('model')
    if 'model' in params and (not isinstance(model, str) or not model.strip()):
        raise RuntimeStoreError('invalid_params')
    cwd = params.get('cwd', str(resolve_agent_cwd()))
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise RuntimeStoreError('invalid_params')
    config = json.loads(json.dumps(config))
    from urllib.parse import urlsplit
    from hermes_constants import parse_reasoning_effort
    for key in ('provider', 'base_url'):
        if key in params:
            value = params[key]
            if not isinstance(value, str) or not value.strip():
                raise RuntimeStoreError('invalid_params')
            if key == 'base_url':
                url = urlsplit(value)
                if (url.scheme not in {'http', 'https'} or not url.hostname
                        or url.username or url.password or url.query or url.fragment):
                    raise RuntimeStoreError('invalid_params')
            config.setdefault('model', {})[key] = value
    if 'ignore_rules' in params and type(params['ignore_rules']) is not bool:
        raise RuntimeStoreError('invalid_params')
    if 'max_turns' in params:
        value = params['max_turns']
        if type(value) is not int or value <= 0:
            raise RuntimeStoreError('invalid_params')
        config.setdefault('agent', {})['max_turns'] = value
    if 'reasoning' in params:
        if not isinstance(params['reasoning'], str) or parse_reasoning_effort(params['reasoning']) is None:
            raise RuntimeStoreError('invalid_params')
        config.setdefault('agent', {})['reasoning_effort'] = params['reasoning']
        # An explicit launch level wins over a per-model default, just like CLI.
        config['agent'].pop('reasoning_overrides', None)
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
    request = {k: v for k, v in params.items() if k not in {'request_id', 'api_key'}}
    request.setdefault('source', 'cli')
    return LocalSessionPolicy(source, SURFACES[source], cwd, model, tuple(sorted(enabled)),
                              json.dumps(config), json.dumps(request, sort_keys=True), json.dumps(terminal))


def bind_launch_key(authority, session_id, policy, api_key):
    """CLI keys live only in this authority lifetime, never its durable receipt.

    Restart deliberately revokes them. History remains readable; inference must
    report launch_credentials_unavailable, never select a profile fallback key.
    """
    from dataclasses import replace
    import hmac
    if api_key is None:
        return policy
    keys = getattr(authority, '_local_launch_keys', None)
    if keys is None:
        keys = authority._local_launch_keys = {}
    ref = f'{authority.instance_id}:{authority.epoch}:{session_id}'
    old = keys.get(ref)
    if old is not None and not hmac.compare_digest(old, api_key):
        raise RuntimeStoreError('admission_conflict')
    keys[ref] = api_key
    return replace(policy, credential_ref=ref)


def launch_key(authority, policy):
    if policy.credential_ref is None:
        return None
    value = getattr(authority, '_local_launch_keys', {}).get(policy.credential_ref)
    if value is None:
        raise RuntimeStoreError('launch_credentials_unavailable')
    return value


def restore_policy(data):
    """Reject incomplete private policy rather than rebuilding from current defaults."""
    try:
        policy = LocalSessionPolicy(**data)
        if (policy.source not in SURFACES or policy.platform != SURFACES[policy.source]
                or not isinstance(policy.cwd, str) or not Path(policy.cwd).is_absolute()
                or not Path(policy.cwd).is_dir()
                or not isinstance(policy.model, str) or not policy.model.strip()
                or not isinstance(policy.toolsets, (list, tuple))
                or any(not isinstance(name, str) for name in policy.toolsets)):
            raise ValueError('invalid policy')
        from dataclasses import replace
        for value in (policy.config_json, policy.request_json, policy.terminal_json):
            if not isinstance(json.loads(value), dict):
                raise ValueError('invalid policy object')
        if json.loads(policy.terminal_json).get('TERMINAL_CWD') != policy.cwd:
            raise ValueError('terminal policy mismatch')
        return replace(policy, toolsets=tuple(policy.toolsets))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeStoreError('storage_unavailable') from exc


def policy_for_source(runner, source):
    from gateway.session_local import LocalSessionAdapter
    from gateway.config import Platform
    if source.platform != Platform.LOCAL:
        return None
    adapter = runner._adapter_for_source(source)
    if isinstance(adapter, LocalSessionAdapter) and adapter.authorize_source(source):
        policy = adapter.policies.get(source.chat_id)
        if policy is None:
            # Cold recovery must restore the frozen policy before executing, not
            # reinterpret a LOCAL route as a default CLI launch.
            raise RuntimeStoreError('storage_unavailable')
        return policy
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
