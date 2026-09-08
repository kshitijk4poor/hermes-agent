"""Private native grants authenticate HTTP, never another purpose or profile."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tests.gateway.test_normal_runtime_boot import control


@contextmanager
def daemon(tmp_path):
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700, exist_ok=True)
    user.mkdir(exist_ok=True)
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'native-http-fixture',
                  'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               PYTHONPATH=str(root), PYTHONUNBUFFERED='1',
               HERMES_DASHBOARD_SESSION_TOKEN='normal-http-owner')
    with (tmp_path / 'gateway.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            descriptor = {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            log.seek(0)
            assert descriptor.get('state') == 'ready', (descriptor, log.read())
            yield home, descriptor
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def ticket(home, descriptor, purpose='native-http', **overrides):
    return control(home, 'session-ticket', {
        'profile_id': str(home.resolve()), 'instance_id': descriptor['instance_id'],
        'purpose': purpose, **overrides,
    })['ticket']


def headers(token):
    return {'X-Hermes-Gateway-Ticket': token}


async def reject_ws(origin, token):
    with pytest.raises(InvalidStatus):
        async with connect(origin.replace('http:', 'ws:') + '/api/ws', subprotocols=[
                'hermes-gateway-v1', 'hermes-gateway-ticket.' + token]):
            pass


@pytest.mark.linux_only
def test_native_http_grants_are_single_use_and_bound_to_daemon(tmp_path):
    with daemon(tmp_path) as (home, descriptor), httpx.Client(
            base_url=descriptor['api_origin'], trust_env=False) as client:
        assert client.get('/api/config').status_code == 401
        normal = {'Authorization': 'Bearer normal-http-owner'}
        assert client.get('/api/config', headers=normal).status_code == 200
        first = ticket(home, descriptor)
        response = client.get('/api/config', headers=headers(first))
        assert response.status_code == 200, response.text
        assert 'native-http-fixture' in response.text
        assert client.get('/api/config', headers=headers(first)).status_code == 401
        assert client.get('/api/config', headers={**normal, **headers('invalid')}).status_code == 401
        for origin in ('https://evil.example', '', 'null'):
            assert client.get('/api/config', headers={**headers(ticket(home, descriptor)),
                                                     'Origin': origin}).status_code == 401
        assert client.get('/api/config', headers=[
            ('X-Hermes-Gateway-Ticket', ticket(home, descriptor)),
            ('X-Hermes-Gateway-Ticket', ticket(home, descriptor)),
        ]).status_code == 401
        for purpose in ('interactive', 'exposure', 'worker-adoption'):
            assert client.get('/api/config', headers=headers(ticket(home, descriptor, purpose))).status_code == 401
        asyncio.run(reject_ws(descriptor['api_origin'], ticket(home, descriptor)))
        for overrides in ({'instance_id': 'stale'}, {'profile_id': str(tmp_path / 'other')}):
            with pytest.raises(AssertionError, match='PermissionError'):
                ticket(home, descriptor, **overrides)
        for path in ('/api/config?profile=other', '/api/config?profile=current&profile=other',
                     '/api/profiles/other/soul'):
            assert client.get(path, headers=headers(ticket(home, descriptor))).status_code == 403
        assert client.post('/api/config', json={'profile': 'other', 'config': {}},
                           headers=headers(ticket(home, descriptor))).status_code == 403
        assert client.get('/api/config?profile=current',
                          headers=headers(ticket(home, descriptor))).status_code == 200
        assert client.get('/api/profiles', headers=headers(ticket(home, descriptor))).status_code == 200
        query_ticket = ticket(home, descriptor)
        assert client.get('/api/config', params={'ticket': query_ticket}).status_code == 401
        assert client.get('/api/config', headers={'Cookie': 'X-Hermes-Gateway-Ticket=' + query_ticket}).status_code == 401
        expired = ticket(home, descriptor)
        time.sleep(31)  # Real production TTL, not a patched clock or shortened grant.
        assert client.get('/api/config', headers=headers(expired)).status_code == 401
        stale = ticket(home, descriptor)
        old_instance = descriptor['instance_id']
        assert 'ticket' not in json.dumps(control(home, 'identify')).lower()
    with daemon(tmp_path) as (home, descriptor), httpx.Client(
            base_url=descriptor['api_origin'], trust_env=False) as client:
        assert descriptor['instance_id'] != old_instance
        assert client.get('/api/config', headers=headers(stale)).status_code == 401
        assert client.get('/api/config', headers=headers(ticket(home, descriptor))).status_code == 200
    print(json.dumps({'native_http': 'ordinary daemon config and profiles HTTP passed',
                      'replay_expiry_restart_purpose_origin_profile': 'rejected'}))
