"""Real summary preparation commits only against the unchanged local transcript."""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest
from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket
from tests.gateway.test_session_mutation_model import CatalogModel


class SummaryModel(CatalogModel):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body.get('messages'):
            self.server.requests.append(body)
        summary = body.get('model') == 'summary'
        text = 'SUMMARY_RETAINED_FACTS' if summary else 'ORDINARY_REPLY ' + ('historical detail ' * 700)
        payload = json.dumps({'id': 'fixture', 'choices': [{'index': 0,
            'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode()
        kind = 'application/json'
        if body.get('stream'):
            payload = ('data: ' + json.dumps({'id': 'fixture', 'choices': [{'index': 0,
                'delta': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.linux_only
def test_compress_runs_summary_and_next_inference_on_atomic_successor(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), SummaryModel)
    peer.requests = []
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    cfg = {'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'original', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False},
                      'compression': {'provider': 'custom', 'model': 'summary', 'base_url': url}},
        'platform_toolsets': {'cli': []}, 'compression': {'protect_first_n': 1, 'protect_last_n': 2}}
    (home / 'config.yaml').write_text(json.dumps(cfg))
    (home / 'models_dev_cache.json').write_text(json.dumps({'custom': {'id': 'custom', 'models': {
        name: {'id': name, 'name': name, 'limit': {'context': 1000000, 'output': 4000}}
        for name in ('original', 'summary')}}}))
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
        OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')

    async def run(desc):
        async with websocket(home, desc) as ws:
            async def call(method, **params):
                response = await rpc(ws, method, **params)
                assert 'result' in response, response
                return response['result']
            sid = (await call('session.create', source='cli', request_id='owner', toolsets=[]))['session_id']
            async def turn(text):
                await call('prompt.submit', session_id=sid, input_id=text, text=text)
                async with asyncio.timeout(30):
                    while True:
                        with sqlite3.connect(home / 'state.db') as db:
                            row = db.execute('SELECT status FROM session_admissions WHERE request_id=?', (text,)).fetchone()
                        if row and row[0] == 'terminal':
                            return await call('session.resume', session_id=sid)
                        await asyncio.sleep(.03)
            for i in range(12):
                before = await turn('HISTORY_' + str(i))
            params = dict(session_id=sid, request_id='compress', operation='compress', payload={},
                expected_revision=before['revision'], expected_generation=before['execution_generation'])
            result = await call('session.mutate', **params)
            assert any(r['model'] == 'summary' for r in peer.requests)
            assert result['target_session_id'] != sid
            compressed = await call('session.resume', session_id=sid)
            assert 'SUMMARY_RETAINED_FACTS' in json.dumps(compressed['messages'])
            await turn('AFTER_COMPRESSION')
            assert 'SUMMARY_RETAINED_FACTS' in json.dumps(peer.requests[-1]['messages'])
            count = len(peer.requests)
            assert await call('session.mutate', **params) == result
            assert len(peer.requests) == count
            with sqlite3.connect(home / 'state.db') as db:
                assert db.execute('SELECT end_reason FROM sessions WHERE id=?', (sid,)).fetchone()[0] == 'compression'
                assert db.execute('SELECT count(*) FROM messages WHERE session_id=?', (sid,)).fetchone()[0] > 0
                receipt = json.loads(db.execute('SELECT value FROM state_meta WHERE key=?',
                    ('gateway.local_policy.v1:' + sid,)).fetchone()[0])
                assert receipt['entry']['session_id'] == result['target_session_id']
            return params, result
    try:
        with daemon(root, home, env, barrier=False) as (_, desc):
            params, result = asyncio.run(run(desc))
        async def retry(desc):
            async with websocket(home, desc) as ws:
                assert (await rpc(ws, 'session.mutate', **params)).get('result') == result
        with daemon(root, home, env, barrier=False) as (_, desc):
            asyncio.run(retry(desc))
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
