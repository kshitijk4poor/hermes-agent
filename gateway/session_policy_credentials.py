"""Borrow frozen config credentials from the existing profile source, not a vault."""
import hashlib
import hmac
import json
from dataclasses import asdict
from pathlib import Path

from agent.credential_persistence import fingerprint_secret_value
from hermes_state_runtime import RuntimeStoreError

PREFIX = 'profile-config-v1:'


def policy_identity(policy):
    data = asdict(policy)
    data.pop('credential_ref')
    data.pop('config_secret_ref')
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def config_reference(authority, session_id, policy, secrets):
    home = Path(authority.db.db_path).resolve().parent
    return PREFIX + json.dumps({'home': str(home), 'profile': authority.profile_id,
        'session': session_id, 'policy': policy_identity(policy),
        'entries': [[list(path), fingerprint_secret_value(value)] for path, value in secrets.items()]},
        sort_keys=True)


def recover_config_secrets(authority, policy):
    ref = policy.config_secret_ref
    if not ref.startswith(PREFIX):
        values = getattr(authority, '_local_config_secrets', {}).get(ref)
        if values is None:
            raise RuntimeStoreError('launch_credentials_unavailable')
        return values
    try:
        source = json.loads(ref[len(PREFIX):])
        home = Path(authority.db.db_path).resolve().parent
        if (source['home'] != str(home) or source['profile'] != authority.profile_id
                or source['policy'] != policy_identity(policy)):
            raise ValueError('scope mismatch')
        cached = getattr(authority, '_local_config_secrets', {}).get(ref)
        if cached is not None:
            return cached
        from gateway.run import _load_gateway_config
        config = _load_gateway_config(home / 'config.yaml')
        values = {}
        for path, fingerprint in source['entries']:
            value = config
            for key in path:
                value = value[key]
            actual = fingerprint_secret_value(value)
            if actual is None or not hmac.compare_digest(actual, fingerprint):
                raise ValueError('credential changed')
            values[tuple(path)] = value
        return values
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise RuntimeStoreError('launch_credentials_unavailable') from exc
