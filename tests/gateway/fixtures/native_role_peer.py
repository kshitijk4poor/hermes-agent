"""Owned Discord SDK peer + actual public adapter/TurnRunner/HTTP execution."""
import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import traceback
from types import SimpleNamespace

from shared_authority_peer import ModelPeer


async def probe(peer):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from plugins.platforms.discord.adapter import DiscordAdapter
    from hermes_state_runtime import list_session_admissions

    class Guild:
        id = 100
        roles = [SimpleNamespace(id=700)]
        calls = 0

        async def fetch_member(self, uid):
            assert uid == 200
            # No SQLite transaction may span the network await.
            assert not authority.db._conn.in_transaction
            self.calls += 1
            await asyncio.sleep(0)
            return SimpleNamespace(id=uid, guild=self, roles=list(self.roles))

        def get_member(self, uid):
            raise AssertionError('cached membership is not authorization')

    class Adapter(DiscordAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.deliveries.append(content)
            return SendResult(success=True, message_id='role-result')

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None):
            pass

        async def get_chat_info(self, chat_id):
            return {'id': chat_id}

    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='role-peer')
    adapter = Adapter(PlatformConfig(enabled=True, token='owned-discord-peer'))
    adapter.deliveries = []
    guild = Guild()
    adapter._client = SimpleNamespace(get_guild=lambda gid: guild if gid == guild.id else None)
    adapter.gateway_runner = runner
    runner.adapters[Platform.DISCORD] = adapter
    runner._wire_adapter_handlers(adapter)

    def event(identity, *, scope='100', role=True):
        return MessageEvent(text='ROLE_INPUT_' + identity, message_id=identity, source=adapter.build_source(
            chat_id='300', chat_type='group', user_id='200', scope_id=scope, role_authorized=role))

    async def send(ev):
        await adapter.handle_message(ev)
        async with asyncio.timeout(15):
            while adapter._active_sessions:
                await asyncio.sleep(0.01)

    ev = event('positive')
    await send(ev)
    entry = runner.session_store.get_or_create_session(ev.source)
    rows = list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)
    assert rows and rows[0]['outcome'] == 'completed', (rows, adapter.deliveries)
    assert len(peer.requests) == 1 and guild.calls >= 2, (peer.requests, guild.calls)
    guild.roles = []
    await send(event('revoked'))
    await send(event('wrong-guild', scope='999'))
    assert len(list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False)) == 1
    assert len(peer.requests) == 1
    print(json.dumps({'model_calls': len(peer.requests), 'fresh_fetches': guild.calls,
                      'outcome': rows[0]['outcome'], 'negative': ['revoked', 'wrong-guild']}))


if __name__ == '__main__':
    peer = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
    peer.requests, peer.metadata_requests = [], []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    base_url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='explicit-loopback-fixture', OPENAI_BASE_URL=base_url,
                      DISCORD_ALLOWED_ROLES='700')
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'model:\n  default: local-wire-stub\n  provider: custom\n  base_url: {base_url}\n'
        f'auxiliary:\n  title_generation:\n    enabled: false\nterminal:\n  cwd: {os.environ["HERMES_HOME"]}\n')
    status = 0
    try:
        asyncio.run(probe(peer))
    except BaseException:
        traceback.print_exc()
        status = 1
    finally:
        peer.shutdown()
        peer.server_close()
    os._exit(status)
