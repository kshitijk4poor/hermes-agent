"""Real HTTP and WS clients share the owner revision and retry receipt."""
import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.mark.asyncio
async def test_http_and_ws_clients_compete_for_one_revision(tmp_path, monkeypatch):
    from hermes_cli.web_routers.sessions import manage_router
    from hermes_cli import web_server as web
    monkeypatch.setattr(web, '_SESSION_TOKEN', 'fixture-token')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    app = FastAPI()
    app.state.auth_required = False
    app.include_router(manage_router)
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('session-fixture', source='test')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(tmp_path), instance_id='owner', db=db, epoch=epoch)
        authority.sessions['session-fixture'] = LiveSession(None, 'route')
        app.state.session_authority = authority

        @app.websocket('/ws')
        async def ws_route(ws: WebSocket):
            if ws.query_params.get('token') != 'fixture-token':
                await ws.close(code=4401)
                return
            await ws.accept()
            connection = AuthorityConnection(authority, object(), {'user_id': 'dashboard-token'})
            try:
                request = await ws.receive_json()
                await ws.send_json(await connection.dispatch(request))
            finally:
                await connection.close()

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='error'))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as client:
                for _ in range(200):
                    if server.started:
                        break
                    await asyncio.sleep(.01)
                assert server.started
                body = {'request_id': 'edit', 'expected_revision': 0, 'title': 'Title', 'pinned': True}
                first = await client.patch('/api/sessions/session-fixture?token=fixture-token', json=body, headers={'Authorization': 'Bearer fixture-token'})
                assert first.status_code == 200, first.text
                assert first.json()['revision'] == 1
                import websockets
                async with websockets.connect(f'ws://127.0.0.1:{port}/ws?token=fixture-token') as ws:
                    await ws.send(json.dumps({'id': 1, 'method': 'session.mutate', 'params': {
                        'session_id': 'session-fixture', 'request_id': 'competitor', 'expected_revision': 0,
                        'operation': 'sidebar', 'payload': {'title': 'Stale'}}}))
                    competing = json.loads(await ws.recv())
                    assert competing['error']['message'] == 'revision_conflict'
                retry = await client.patch('/api/sessions/session-fixture?token=fixture-token', json=body, headers={'Authorization': 'Bearer fixture-token'})
                assert retry.json() == first.json()
                denied = await client.patch('/api/sessions/session-fixture', json=body)
                assert denied.status_code == 401
                foreign = await client.patch('/api/sessions/session-fixture?token=fixture-token', json=body | {'profile': 'other'}, headers={'Authorization': 'Bearer fixture-token'})
                assert foreign.status_code in (400, 403, 404)
                assert db.get_session('session-fixture')['runtime_revision'] == 1
                assert db.get_session('session-fixture')['title'] == 'Title'
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            listener.close()
