import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_private_control_peer_mints_profile_bound_ticket(tmp_path):
    from gateway.control_socket import GatewayControlServer, resolve_client_socket_path
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    server = GatewayControlServer(home)
    # The actual service must implement this closed verb; unknown-verb is the pre-feature RED.
    try:
        from gateway.runtime_bootstrap import TicketStore
        server.ticket_store = TicketStore('instance', frozenset({str(home.resolve())}))
    except ImportError:
        pass
    assert await server.start()
    async def request(params, **extra):
        reader, writer = await asyncio.open_unix_connection(str(resolve_client_socket_path(home)))
        writer.write(json.dumps({'protocol': 1, 'verb': 'session-ticket', 'params': params, **extra}).encode() + b'\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
        writer.close()
        await writer.wait_closed()
        return reply
    params = {'profile_id': str(home.resolve()), 'instance_id': 'instance', 'purpose': 'interactive'}
    try:
        reply = await request(params)
        assert reply['ok'], reply
        ticket = reply['result']['ticket']
        grant = server.ticket_store.redeem(ticket, profile_id=params['profile_id'], purpose='interactive')
        assert grant['subject'] == f'uid:{os.getuid()}'
        assert grant['instance_id'] == 'instance'
        assert 'session:create' in grant['capabilities']
        for invalid in ({**params, 'instance_id': 'old'}, {**params, 'profile_id': '/wrong'}, {**params, 'subject': 'admin'}):
            assert not (await request(invalid))['ok']
        assert not (await request(params, origin='http://browser'))['ok']
        # Calling the dispatcher directly does not establish peer identity.
        assert not json.loads(server.handle_request_line(json.dumps({'verb': 'session-ticket', 'params': params}).encode()))['ok']
    finally:
        await server.stop()


def test_ticket_atomic_single_use_profile_purpose_expiry_and_capacity(monkeypatch):
    from gateway.runtime_bootstrap import TicketStore
    clock = [0.0]
    monkeypatch.setattr('gateway.runtime_bootstrap.time.monotonic', lambda: clock[0])
    store = TicketStore('boot', frozenset({'a', 'b'}))
    ticket = store.mint(profile_id='a', subject='uid:1', purpose='interactive')
    for profile, purpose in [('b', 'interactive'), ('a', 'exposure')]:
        with pytest.raises(PermissionError):
            store.redeem(ticket, profile_id=profile, purpose=purpose)
    def consume(_):
        try:
            return store.redeem(ticket, profile_id='a', purpose='interactive')
        except PermissionError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        grants = list(pool.map(consume, range(8)))
    assert sum(g is not None for g in grants) == 1
    expired = store.mint(profile_id='a', subject='uid:1', purpose='interactive')
    clock[0] = 30
    with pytest.raises(PermissionError):
        store.redeem(expired, profile_id='a', purpose='interactive')
    exposure = store.mint(profile_id='a', subject='uid:1', purpose='exposure')
    assert store.redeem(exposure, profile_id='a', purpose='exposure')['capabilities'] == frozenset({'transport:delegate'})
    for _ in range(store.MAX_ENTRIES):
        store.mint(profile_id='a', subject='uid:1', purpose='interactive')
    with pytest.raises(PermissionError):
        store.mint(profile_id='a', subject='uid:1', purpose='interactive')
