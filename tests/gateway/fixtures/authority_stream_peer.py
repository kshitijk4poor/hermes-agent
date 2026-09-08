"""Real SSE execution and authenticated authority observers in a disposable process."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import threading
import traceback


class ModelPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = request.get('messages', [])
        if not messages:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
            return
        self.server.requests.append(request)
        tool = not any(m['role'] == 'tool' for m in messages)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        assert request.get('stream'), 'fixture requires actual streaming model request'
        deltas = [{'role': 'assistant', 'reasoning_content': 'PRIVATE_REASONING_NOT_PUBLIC'}]
        if tool:
            deltas += [{'content': 'Working '}, {'content': 'on the fixture.'}, {'tool_calls': [{
                'index': 0, 'id': 'owned-tool', 'type': 'function', 'function': {
                    'name': 'terminal', 'arguments': json.dumps({'command': self.server.command})}}]}]
        else:
            deltas += [{'content': 'STREAM_'}, {'content': 'FINISHED'}]
        for delta in deltas + [{}]:
            frame = {'id': 'local-stream', 'object': 'chat.completion.chunk', 'created': 1,
                     'model': 'local-stream', 'choices': [{'index': 0, 'delta': delta,
                     'finish_reason': None if delta else ('tool_calls' if tool else 'stop')}]}
            self.wfile.write(('data: ' + json.dumps(frame) + '\n\n').encode())
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


class Observer:
    def __init__(self, ws, sid):
        self.ws, self.sid = ws, sid
        self.events, self.waiters = [], {}
        self.complete = asyncio.Event()
        self.task = asyncio.create_task(self.read())
        self.rid = 0

    async def read(self):
        async for raw in self.ws:
            frame = json.loads(raw)
            if frame.get('id') in self.waiters:
                self.waiters.pop(frame['id']).set_result(frame)
            elif frame.get('params', {}).get('session_id') == self.sid:
                self.events.append(frame['params'])
                if frame['params']['type'] == 'message.complete':
                    self.complete.set()

    async def rpc(self, method, **params):
        self.rid += 1
        future = asyncio.get_running_loop().create_future()
        self.waiters[self.rid] = future
        await self.ws.send(json.dumps({'jsonrpc': '2.0', 'id': self.rid, 'method': method,
                                      'params': {'session_id': self.sid, **params}}))
        frame = await asyncio.wait_for(future, 15)
        assert 'result' in frame, frame
        return frame['result']

    async def close(self):
        await self.ws.close()
        await self.task


async def probe(peer, target):
    import websockets
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.run import GatewayRunner
    from gateway.run_api import start_gateway_api, stop_gateway_api
    from gateway.session import SessionSource
    from gateway.session_authority import initialize_session_authority
    from hermes_cli import web_server
    from hermes_state_runtime import list_session_admissions
    from run_agent import AIAgent

    class NativeAdapter(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token='fixture'), Platform.TELEGRAM)
            self.sent = []
        async def connect(self, *, is_reconnect=False):
            return True
        async def disconnect(self):
            pass
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return SendResult(success=True, message_id='fixture')
        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            self.sent.append(content)
            return SendResult(success=True, message_id=message_id)
        async def send_typing(self, chat_id, metadata=None):
            pass
        async def get_chat_info(self, chat_id):
            return {'id': chat_id}

    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='stream')
    adapter = NativeAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    ref = authority.register(SessionSource(platform=Platform.TELEGRAM, chat_id='stream-chat',
                                           chat_type='dm', user_id='fixture-user'))
    api = await start_gateway_api(runner)
    uri = api.api_origin.replace('http:', 'ws:') + '/api/ws'
    observers = []

    async def connect():
        ws = await websockets.connect(uri + '?token=' + web_server._SESSION_TOKEN, compression=None)
        observer = Observer(ws, ref.session_id)
        observers.append(observer)
        return observer

    try:
        try:
            async with websockets.connect(uri):
                raise AssertionError('unauthenticated observer accepted')
        except websockets.exceptions.InvalidStatus as exc:
            assert exc.response.status_code == 403
        a, b = await connect(), await connect()
        snapshot = await a.rpc('session.resume')
        await b.rpc('session.resume')
        receipt = await a.rpc('prompt.submit', input_id='stream-first', text='Write the owned fixture')
        await asyncio.wait_for(a.complete.wait(), 30)
        await asyncio.wait_for(b.complete.wait(), 10)
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 10)
        assert target.read_text() == 'owned-effect'
        agent = authority.agent(ref)
        assert isinstance(agent, AIAgent)
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        assert len(rows) == 1 and rows[0]['outcome'] == 'completed', rows
        expected = {'message.start', 'message.delta', 'tool.start', 'tool.complete', 'agent.step'}
        assert expected <= {e['type'] for e in a.events}, 'real execution omitted canonical start/delta/tool/progress events'
        assert a.events == b.events, 'observers saw competing event ordering/stamps'
        assert [e['seq'] for e in a.events] == list(range(snapshot['last_sequence'] + 1, a.events[-1]['seq'] + 1))
        for event in a.events:
            assert event['session_id'] == ref.session_id
            assert event['execution_generation'] == rows[0]['generation']
            assert event['authority_epoch'] == authority.epoch
            assert event['admission_id'] == receipt['admission_id']
            assert event['replay_epoch'] == snapshot['replay_epoch']
        tools = [e for e in a.events if e['type'].startswith('tool.')]
        assert [e['type'] for e in tools] == ['tool.start', 'tool.complete']
        assert all(e['payload']['tool_call_id'] == 'owned-tool' for e in tools)
        assert 'PRIVATE_REASONING_NOT_PUBLIC' not in json.dumps(a.events)
        assert str(target) not in json.dumps(tools), 'tool args leaked into public stream'
        assert adapter.sent, 'native delivery lane was lost'
        last = a.events[-1]['seq']
        # Same generation is still recorded after settlement; late callbacks must be inert.
        agent.stream_delta_callback('LATE_CALLBACK_NOT_PUBLIC')
        agent.tool_start_callback('late', 'terminal', {'command': 'PRIVATE_ARGS'})
        agent.tool_complete_callback('late', 'terminal', {}, 'PRIVATE_RESULT')
        agent.step_callback(999, [{'name': 'PRIVATE_PREVIOUS_TOOL', 'result': 'SECRET'}])
        replay = await b.rpc('session.events.since', replay_epoch=snapshot['replay_epoch'], last_sequence=0)
        assert replay['last_sequence'] == last, 'settled worker published late callbacks'
        assert replay['events'] == a.events
        cursor = a.events[-2]['seq']
        await a.close()
        observers.remove(a)
        c = await connect()
        await c.rpc('session.resume')
        tail = await c.rpc('session.events.since', replay_epoch=snapshot['replay_epoch'], last_sequence=cursor)
        assert tail['events'] == [a.events[-1]], 'detach/reconnect replay lost terminal tail'
        Path(os.environ['HERMES_HOME'], 'receipt.json').write_text(json.dumps({
            'real_terminal_effect': True, 'authenticated_observers': 2, 'same_stamps': True,
            'stale_callback_inert': True, 'reconnect_replay': True, 'model_calls': len(peer.requests),
            'event_types': [e['type'] for e in a.events], 'event_count': len(a.events)}))
    finally:
        for observer in observers:
            await observer.close()
        await stop_gateway_api(api)


def main():
    target = Path(os.environ['HERMES_HOME'], 'owned-effect.txt')
    peer = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
    peer.requests = []
    peer.command = 'printf owned-effect > ' + shlex.quote(str(target))
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url,
                      TELEGRAM_ALLOWED_USERS='fixture-user', TERMINAL_ENV='local')
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'model:\n  default: local-stream\n  provider: custom\n  base_url: {url}\n'
        'streaming:\n  enabled: true\nauxiliary:\n  title_generation:\n    enabled: false\n')
    try:
        asyncio.run(probe(peer, target))
    finally:
        peer.shutdown()
        peer.server_close()


if __name__ == '__main__':
    status = 0
    try:
        main()
    except BaseException:
        traceback.print_exc()
        status = 1
    os._exit(status)
