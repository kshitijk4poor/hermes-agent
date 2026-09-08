"""The ordinary no-platform process must earn its bootstrap readiness."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


def control(home, verb, params=None):
    pointer = home / 'gateway.sock.path'
    path = pointer.read_text().strip() if pointer.exists() else str(home / 'gateway.sock')
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(2)
        peer.connect(path)
        peer.sendall(json.dumps({'protocol': 1, 'id': 1, 'verb': verb,
                                 'params': params or {}}).encode() + b'\n')
        with peer.makefile('rb') as stream:
            reply = json.loads(stream.readline())
    assert reply['ok'], reply
    return reply['result']


async def handshake(home, descriptor):
    url = descriptor['api_origin'].replace('http:', 'ws:') + '/api/ws'
    with pytest.raises(InvalidStatus):
        async with connect(url):
            pass
    binding = {'profile_id': str(home.resolve()), 'instance_id': descriptor['instance_id'],
               'purpose': 'interactive'}
    grant = control(home, 'session-ticket', binding)
    protocols = ['hermes-gateway-v1', 'hermes-gateway-ticket.' + grant['ticket']]
    async with connect(url, subprotocols=protocols) as ws:
        await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': 'session.resume',
                                  'params': {'session_id': 'not-registered'}}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if reply.get('id') == 7:
                break
        assert reply['error']['message'] == 'not_found', reply
        assert ws.subprotocol == 'hermes-gateway-v1'
    with pytest.raises(InvalidStatus):
        async with connect(url, subprotocols=protocols):
            pass


@pytest.mark.linux_only
def test_normal_entrypoint_earns_authenticated_authority_readiness(tmp_path):
    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    user = tmp_path / 'user'
    user.mkdir()
    (home / 'config.yaml').write_text('gateway:\n  multiplex_profiles: false\nauxiliary:\n  title_generation:\n    enabled: false\n')
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               PYTHONPATH=str(root), PYTHONUNBUFFERED='1')
    with (tmp_path / 'gateway.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        descriptor = {}
        try:
            deadline = time.monotonic() + 35
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            log.flush()
            log.seek(0)
            assert descriptor.get('state') == 'ready', (descriptor, log.read())
            assert descriptor['authority_epoch'] > 0
            assert descriptor['served_profiles'] == [{'profile_id': str(home), 'home': str(home)}]
            assert 'session-authority-v1' in descriptor['capabilities']
            asyncio.run(handshake(home, descriptor))
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=20) == 0
            assert not (home / 'gateway.pid').exists()
            assert not (home / 'gateway.sock').exists()
            print(json.dumps({k: descriptor[k] for k in ('instance_id', 'authority_epoch', 'state', 'capabilities')}))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.asyncio
async def test_api_bind_failure_unwinds_control_db_and_reservation(tmp_path, monkeypatch):
    from functools import partial
    from gateway import run_api
    from gateway.run import start_gateway
    from gateway.config import GatewayConfig
    from gateway.runtime_ownership import process_ownership
    from hermes_cli import web_server as web
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1', 0))
        blocker.listen()
        monkeypatch.setattr(run_api, 'start_gateway_api', partial(
            run_api.start_gateway_api, port=blocker.getsockname()[1]))
        with pytest.raises(OSError):
            await start_gateway(GatewayConfig(), verbosity=None)
        assert getattr(web.app.state, 'gateway_runner', None) is None
        assert not (home / 'gateway.pid').exists()
        assert not (home / 'gateway.sock').exists()
        assert not process_ownership.owns(home)
