"""The ordinary daemon, not a fixture launcher, owns the real tool worker."""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not body.get('messages'):
            message = {'role': 'assistant', 'content': 'metadata'}
        elif any(m['role'] == 'tool' for m in body['messages']):
            self.server.requests.append(body)
            self.server.blocked.set()
            self.server.release.wait(30)
            message = {'role': 'assistant', 'content': 'MANAGED_TOOL_DONE'}
        else:
            self.server.requests.append(body)
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'managed-tool', 'type': 'function', 'function': {'name': 'terminal',
                'arguments': json.dumps({'command': 'printf MANAGED_TOOL_EFFECT', 'timeout': 10})}}]}
        choice = {'index': 0, 'message': message, 'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}
        frame = {'id': 'managed-model', 'model': 'managed-model', 'choices': [choice],
                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        kind = 'application/json'
        if body.get('stream'):
            choice['delta'] = choice.pop('message')
            for index, call in enumerate(choice['delta'].get('tool_calls', [])):
                call['index'] = index
            payload = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            payload = json.dumps(frame).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.mark.linux_only
def test_ordinary_owner_launches_tool_worker_and_detach_does_not_cancel(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False, 'managed_workers': True},
        'model': {'provider': 'custom', 'default': 'managed-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'cli': ['terminal']}}))
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)

    def query(sql, args=()):
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
            return db.execute(sql, args).fetchall()

    async def exercise(desc, owner):
        import psutil
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='managed', source='cli', cwd=str(home),
                                model='managed-model', provider='custom', base_url=url, api_key='loopback-only',
                                toolsets=['terminal'], ignore_rules=True)
            assert 'result' in created, created
            sid = created['result']['session_id']
            submitted = await rpc(ws, 'prompt.submit', session_id=sid, input_id='managed-input', text='DO_MANAGED_TOOL')
            assert 'result' in submitted, submitted
            assert await asyncio.to_thread(peer.blocked.wait, 30), (home / 'restart.log').read_text()
            workers = query('SELECT execution_id,status FROM worker_executions WHERE session_id=?', (sid,))
            assert len(workers) == 1, workers
            children = [p for p in psutil.Process(owner.pid).children() if p.cmdline()[-2:] == ['-m', 'agent.managed_worker']]
            assert len(children) == 1, [(p.pid, p.cmdline()) for p in psutil.Process(owner.pid).children()]
            pid = children[0].pid
        # Viewer connection is gone while the real model still holds the turn.
        assert psutil.Process(pid).is_running()
        peer.release.set()
        async with asyncio.timeout(40):
            while query('SELECT status FROM session_admissions WHERE request_id=?', ('managed-input',)) != [('terminal',)]:
                await asyncio.sleep(.05)
        async with websocket(home, desc) as ws:
            restored = await rpc(ws, 'session.resume', session_id=sid)
            history = restored['result']['messages']
            assert 'MANAGED_TOOL_DONE' in json.dumps(history), restored
        rows = query('SELECT role,content FROM messages WHERE session_id=? ORDER BY id', (sid,))
        assert sum(role == 'user' and 'DO_MANAGED_TOOL' in content for role, content in rows) == 1, rows
        assert sum(role == 'assistant' and 'MANAGED_TOOL_DONE' in (content or '') for role, content in rows) == 1, rows
        assert any(role == 'tool' and 'MANAGED_TOOL_EFFECT' in content for role, content in rows), rows
        assert query('SELECT status FROM worker_executions WHERE session_id=?', (sid,)) == [('terminal',)]
        assert query('SELECT COUNT(*) FROM session_turn_leases') == [(0,)]
        assert len(peer.requests) == 2
        print(json.dumps({'owner_pid': owner.pid, 'worker_pid': pid, 'worker_module': 'agent.managed_worker',
                          'model_requests': len(peer.requests), 'rows': rows, 'detach_survived': True}))

    try:
        with daemon(root, home, env, barrier=False) as (owner, desc):
            asyncio.run(exercise(desc, owner))
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
