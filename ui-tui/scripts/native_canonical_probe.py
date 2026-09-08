"""Disposable ordinary-daemon + real Ink PTY probe (no renderer injection)."""
import asyncio
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.gateway.fixtures.shared_authority_peer import ModelPeer
from websockets.asyncio.client import connect


def run():
    receipts = {}
    with tempfile.TemporaryDirectory(prefix='ink-native-authority-') as temp:
        base = Path(temp)
        home = base / 'state'
        home.mkdir(mode=0o700)
        user = base / 'user'
        user.mkdir()
        model = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
        model.requests, model.metadata_requests = [], []
        threading.Thread(target=model.serve_forever, daemon=True).start()
        model_url = f'http://127.0.0.1:{model.server_port}/v1'
        (home / 'config.yaml').write_text(json.dumps({
            'gateway': {'multiplex_profiles': False},
            'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': model_url},
            'auxiliary': {'title_generation': {'enabled': False}},
        }))
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
        env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
                   PYTHONPATH=str(ROOT), PYTHONUNBUFFERED='1', HERMES_PYTHON=sys.executable,
                   HERMES_PYTHON_SRC_ROOT=str(ROOT), TERM='xterm-256color',
                   OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=model_url)
        children = []
        daemon = None

        def grant():
            result = subprocess.run([sys.executable, str(ROOT / 'ui-tui/scripts/gateway_bootstrap.py')],
                                    cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        async def rpc(ws, method, **params):
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': method, 'method': method, 'params': params}))
            async with asyncio.timeout(15):
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get('id') == method:
                        assert 'result' in reply, reply
                        return reply['result']

        async def seed():
            g = grant()
            async with connect(g['url'], subprotocols=g['protocols']) as ws:
                description = await rpc(ws, 'runtime.describe')
                source = 'tui' if 'tui' in description['session_create']['sources'] else 'cli'
                result = await rpc(ws, 'session.create', request_id='native-seed', source=source)
                receipts['seed_source'] = source
                receipts['session_id'] = result['session_id']
                return result['session_id']

        def launch(name, sid=None, query=None):
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 120, 0, 0))
            launch_env = dict(env)
            launch_env['HERMES_TUI_ACTIVE_SESSION_FILE'] = str(base / (name + '.json'))
            if sid:
                launch_env['HERMES_TUI_RESUME'] = sid
            if query:
                launch_env['HERMES_TUI_QUERY'] = query
            proc = subprocess.Popen(['node', str(ROOT / 'ui-tui/dist/entry.js')], cwd=ROOT,
                                    env=launch_env, stdin=slave, stdout=slave, stderr=slave,
                                    start_new_session=True)
            os.close(slave)
            children.append((proc, master))
            output = bytearray()
            def drain():
                while proc.poll() is None:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
            threading.Thread(target=drain, daemon=True).start()
            return proc, master, output

        try:
            log = (base / 'daemon.log').open('w+')
            daemon = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=ROOT, env=env,
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                try:
                    g = grant()
                    break
                except AssertionError:
                    assert daemon.poll() is None, 'daemon exited'
                    time.sleep(.1)
            else:
                raise AssertionError('daemon bootstrap readiness timeout')
            receipts['instance_id'] = g['instance_id']
            sid = asyncio.run(seed())
            first = launch('first', sid, 'WS_SHARED')
            second = launch('second', sid)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline and (not model.requests or b'LOCAL_ACK' not in first[2]):
                assert first[0].poll() is None, bytes(first[2]).decode(errors='replace')
                time.sleep(.1)
            receipts['model_requests'] = len(model.requests)
            receipts['first_rendered_reply'] = b'LOCAL_ACK' in first[2]
            receipts['second_rendered_reply'] = b'LOCAL_ACK' in second[2]
            for name, child in [('first', first), ('second', second)]:
                (Path(sys.argv[1]) / (name + '.pty')).write_bytes(child[2])
            assert receipts['first_rendered_reply'], bytes(first[2]).decode(errors='replace')[-12000:]
            assert receipts['second_rendered_reply'], bytes(second[2]).decode(errors='replace')[-12000:]
            first[0].terminate()
            second[0].terminate()
            first[0].wait(timeout=5)
            second[0].wait(timeout=5)
            third = launch('reconnected', sid)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and b'LOCAL_ACK' not in third[2]:
                time.sleep(.1)
            receipts['reconnect_rendered_reply'] = b'LOCAL_ACK' in third[2]
            receipts['daemon_survived_detach'] = daemon.poll() is None
            assert receipts['reconnect_rendered_reply']
        finally:
            for proc, master in children:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                os.close(master)
            if daemon and daemon.poll() is None:
                daemon.send_signal(signal.SIGINT)
                try:
                    daemon.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
            model.shutdown()
            model.server_close()
            (Path(sys.argv[1]) / 'receipt.json').write_text(json.dumps(receipts, indent=2))
            print(json.dumps(receipts), flush=True)


if __name__ == '__main__':
    Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)
    run()
