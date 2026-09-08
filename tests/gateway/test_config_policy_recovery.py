"""Config credentials borrow only their frozen profile source across restart."""
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest


def test_config_credentials_recover_only_exact_source(tmp_path, monkeypatch):
    from gateway.session_policy import build_policy, bind_launch_key
    from hermes_state_runtime import RuntimeStoreError
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    cfg = {'model': {'provider': 'custom', 'api_key': 'owned-config-token',
                     'base_url': 'http://127.0.0.1:1234/v1'}}
    (home / 'config.yaml').write_text(json.dumps(cfg))
    def owner(path=home, profile='owned', instance='new'):
        return SimpleNamespace(db=SimpleNamespace(db_path=path / 'state.db'),
                               profile_id=profile, instance_id=instance, epoch=2)
    private = {}
    policy = build_policy({'cwd': str(home), 'model': 'frozen'}, cfg, private_secrets=private)
    policy = bind_launch_key(owner(instance='old'), 'session', policy, None, config_secrets=private)
    assert policy.config(owner())['model']['api_key'] == cfg['model']['api_key']
    assert cfg['model']['api_key'] not in json.dumps(asdict(policy))
    for wrong in (owner(profile='wrong'), owner(path=tmp_path)):
        with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
            policy.config(wrong)
    with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
        replace(policy, model='other-policy').config(owner())
    cfg['model']['api_key'] = 'changed-unrelated-token'
    (home / 'config.yaml').write_text(json.dumps(cfg))
    with pytest.raises(RuntimeStoreError, match='launch_credentials_unavailable'):
        policy.config(owner())
