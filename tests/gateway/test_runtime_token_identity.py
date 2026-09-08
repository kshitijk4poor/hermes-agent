"""Legacy token authentication must become an explicit authority principal."""
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


@pytest.mark.asyncio
async def test_authenticated_token_can_create_without_identityless_permissions(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.run_runtime import initialize_gateway_runtime, start_gateway_runtime_api, publish_gateway_runtime_ready
    from gateway.runtime_ownership import process_ownership
    from gateway.session_controls import AuthorityConnection
    from hermes_constants import get_hermes_home
    from hermes_cli import web_server as web

    # Session creation persists a restorable model policy, even without a turn.
    (get_hermes_home() / 'config.yaml').write_text(json.dumps({
        'model': {'provider': 'custom', 'default': 'local-wire-stub',
                  'base_url': 'http://127.0.0.1:1/v1'},
        'platform_toolsets': {'cli': []},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    process_ownership.reserve([get_hermes_home()])
    runner = GatewayRunner(GatewayConfig())
    try:
        await initialize_gateway_runtime(runner)
        await start_gateway_runtime_api(runner)
        assert await runner.start()
        publish_gateway_runtime_ready(runner)
        url = runner.session_api.api_origin.replace('http:', 'ws:') + '/api/ws'
        with pytest.raises(InvalidStatus):
            async with connect(url + '?token=wrong'):
                pass
        async with connect(url + '?token=' + web._SESSION_TOKEN) as ws:
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'session.create',
                                      'params': {'request_id': 'token-create', 'source': 'cli'}}))
            import asyncio
            async with asyncio.timeout(10):
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get('id') == 1:
                        break
            assert 'result' in reply, reply
        unbound = AuthorityConnection(runner.session_authority, object(), {})
        try:
            assert not unbound.actor.capabilities
        finally:
            await unbound.close()
    finally:
        await runner.stop()
        process_ownership.close()
