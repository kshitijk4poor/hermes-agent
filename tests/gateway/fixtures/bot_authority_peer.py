"""Authenticated production WebSockets and loopback-only inference."""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

from automation_peer import Model
from local_recovery_probe import daemon, websocket, rpc


def probe(base):
    root = Path(__file__).resolve().parents[3]
    home, user = base / 'state', base / 'user'
    home.mkdir(); user.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.requests = []
    model.blocked, model.release = threading.Event(), threading.Event()
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{model.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'local-fixture', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}}}))
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)

    def rows():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM session_admissions ORDER BY seq')]

    async def wait(predicate):
        async with asyncio.timeout(30):
            while not predicate():
                await asyncio.sleep(.05)

    async def run(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='bot', source='gui', toolsets=[], cwd=str(home))
            sid = created['result']['session_id']
            renamed = await rpc(ws, 'session.mutate', session_id=sid, request_id='name',
                expected_revision=created['result']['revision'], operation='rename', payload={'title': 'Bot Chat'})
            assert 'result' in renamed, renamed
            await rpc(ws, 'prompt.submit', session_id=sid, input_id='warm', text='WARM_BOT')
            await wait(lambda: rows()[0]['status'] == 'terminal')
            await rpc(ws, 'prompt.submit', session_id=sid, input_id='hold', text='HOLD_AUTOMATION')
            assert await asyncio.to_thread(model.blocked.wait, 20)
            params = {'id': 'a' * 32, 'profile': 'default', 'message': '[Message from @sender] BOT_DM_ONCE'}
            wrong = await rpc(ws, 'bot_relay.deliver', **{**params, 'profile': 'wrong-profile'})
            assert wrong['error']['message'] == 'profile_mismatch', wrong
            # Discard an actual response at the socket boundary before reconnecting.
            await ws.send(json.dumps({'id': 'lost-ack', 'method': 'bot_relay.deliver', 'params': params}))
            await wait(lambda: any('BOT_DM_ONCE' in r['payload_json'] for r in rows()))
        async with websocket(home, desc) as observer:
            assert 'result' in await rpc(observer, 'session.resume', session_id=sid)
            retry = await rpc(observer, 'bot_relay.deliver', **params)
            assert 'result' in retry, retry
            assert retry['result']['status'] == 'queued', retry
            conflict = await rpc(observer, 'bot_relay.deliver', **{**params, 'message': 'CHANGED'})
            assert conflict['error']['message'] == 'admission_conflict', conflict
            bot = [r for r in rows() if 'BOT_DM_ONCE' in r['payload_json']]
            assert len(bot) == 1 and bot[0]['target_session_id'] == sid and bot[0]['status'] == 'queued', bot
            model.release.set()
            await wait(lambda: all(r['status'] == 'terminal' for r in rows()))
            settled = await rpc(observer, 'bot_relay.deliver', **params)
            assert settled['result']['status'] == 'settled', settled
            assert settled['result']['reply'] == 'AUTOMATION_ACK', settled
        texts = [next((m.get('content', '') for m in reversed(r['messages']) if m['role'] == 'user'), '')
                 for r in model.requests if r.get('messages')]
        assert sum('BOT_DM_ONCE' in str(t) for t in texts) == 1, texts
        print(json.dumps({'ledger': rows(), 'inputs': texts, 'lost_ack': True, 'same_target': sid}))

    try:
        with daemon(root, home, env, barrier=False) as (proc, desc):
            try:
                asyncio.run(run(desc))
                print(json.dumps({'ordinary_daemon_pid': proc.pid}))
            except BaseException:
                print((home / 'restart.log').read_text()[-10000:], file=sys.stderr)
                raise
    finally:
        model.release.set(); model.shutdown(); model.server_close()


if __name__ == '__main__':
    probe(Path(sys.argv[1]))
