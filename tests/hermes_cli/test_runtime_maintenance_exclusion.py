"""Maintenance must not replace the database behind a live authority."""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading

from tests.gateway.fixtures.local_recovery_probe import Model, daemon, rpc, websocket


def test_restore_refuses_live_authority_without_changing_data(tmp_path):
    from hermes_cli.backup import create_quick_snapshot, restore_quick_snapshot

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
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'fixture', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'cli': []},
    }))
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)

    def rows():
        with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
            return list(db.execute('SELECT request_id,status FROM session_admissions ORDER BY rowid'))

    async def exercise(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='create', source='tui', cwd=str(home), toolsets=[])
            session = created['result']['session_id']
            await rpc(ws, 'prompt.submit', session_id=session, input_id='warm', text='WARM')
            async with asyncio.timeout(30):
                while dict(rows()).get('warm') != 'terminal':
                    await asyncio.sleep(.05)
            snapshot = create_quick_snapshot(hermes_home=home)
            assert snapshot, snapshot
            await rpc(ws, 'prompt.submit', session_id=session, input_id='started', text='BLOCK_STARTED')
            assert await asyncio.to_thread(peer.blocked.wait, 25)
            await rpc(ws, 'prompt.submit', session_id=session, input_id='queued', text='FOLLOWER')
            before = rows()
            assert dict(before) == {'warm': 'terminal', 'started': 'started', 'queued': 'queued'}
            restored = restore_quick_snapshot(snapshot, hermes_home=home)
            after = rows()
            print(json.dumps({'live_restore': restored, 'before': before, 'after': after}), flush=True)
            assert not restored and after == before
            return snapshot

    try:
        with daemon(root, home, env, barrier=False) as (proc, desc):
            snapshot = asyncio.run(exercise(desc))
            old_epoch = desc['authority_epoch']
        peer.release.set()
        assert restore_quick_snapshot(snapshot, hermes_home=home)
        assert rows() == [('warm', 'terminal')]
        with daemon(root, home, env, barrier=False) as (_, restarted):
            assert restarted['instance_id'] != desc['instance_id']
            assert restarted['authority_epoch'] > old_epoch
            assert rows() == [('warm', 'terminal')]
    finally:
        peer.release.set()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
