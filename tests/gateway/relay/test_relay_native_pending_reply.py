"""A typed relay reply answers the prompt the running turn is blocked on.

With a session authority, ``RelayAdapter._on_inbound`` commits a plain chat message straight to the
durable FIFO (``admit_relay_delivery`` -> ``admit_producer``) and never runs
``GatewayRunner._handle_message``, where ``_hm_pending_reply_intercepts`` turns a typed message into
the answer of a pending clarify ("Other (type answer)", open-ended questions), and where a bare
"yes" answers a blocking approval. The answer was queued behind the very turn that waited for it,
so the turn sat out the whole prompt timeout (an hour by default) before the reply ran as a new turn.

Real ``GatewayRunner`` + session authority + ``RelayAdapter`` + ``WebSocketRelayTransport`` against a
fixture connector; only the model call is stubbed, and it blocks on the real clarify / approval
primitive.
"""
import asyncio

import pytest

from gateway.session_results import execution_result
# Its import skips this module too when ``websockets`` is missing (``importorskip`` at import time).
from tests.gateway.relay.test_relay_native_turn_lifecycle import _actions, _gateway, _stub_model
from tests.waiting import async_wait_until


def _sent(outgoing, text):
    return any(a.get('op') == 'send' and a.get('content') == text for a in _actions(outgoing))


@pytest.fixture
def dispatch_hook(request):
    """``with-hook``: a ``pre_gateway_dispatch`` plugin is registered, so ``admit_producer`` takes its
    prepared-payload branch (``_admit_once``) instead of the bare one."""
    if request.param != 'with-hook':
        yield
        return
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    discovered, callbacks = manager._discovered, manager._hooks.setdefault('pre_gateway_dispatch', [])
    manager._discovered = True
    callbacks.append(lambda **_kwargs: None)
    try:
        yield
    finally:
        callbacks.pop()
        manager._discovered = discovered


@pytest.mark.asyncio
@pytest.mark.parametrize('dispatch_hook', ['bare', 'with-hook'], indirect=True)
@pytest.mark.parametrize('choices', [None, ['Telegram', 'Discord']], ids=['open-ended', 'other-picked'])
async def test_typed_reply_answers_the_pending_clarify_instead_of_queueing(tmp_path, monkeypatch, choices,
                                                                           dispatch_hook):
    from tools import clarify_gateway

    async with _gateway(tmp_path, 'relay-clarify-typed') as (runner, authority, send, outgoing, _adapter):
        pending = {}

        async def ask(event):
            key = runner._session_key_for_source(event.source)
            entry = clarify_gateway.register('cq1', key, 'Which platform?', choices)
            if choices:
                clarify_gateway.mark_awaiting_text('cq1')  # the user pressed "Other (type answer)"
            pending['key'] = key
            answer = await asyncio.to_thread(clarify_gateway.wait_for_response, entry.clarify_id, 20)
            text = f'answered {answer}'
            execution_result.get()['result'] = {'final_response': text, 'messages': []}
            return text

        executed = _stub_model(runner, monkeypatch, {'help me get set up': ask})
        try:
            # One Slack thread is one session (a top-level Slack message opens its own thread).
            await send('1700000000.000100', 'D1', 'help me get set up', thread_id='1700000000.000001')
            await async_wait_until(lambda: 'key' in pending and clarify_gateway.has_pending(pending['key']),
                                   timeout=20, what='the turn blocked on clarify')
            await send('1700000000.000200', 'D1', 'can i set up multiple bots', thread_id='1700000000.000001')
            await async_wait_until(lambda: _sent(outgoing, 'answered can i set up multiple bots'), timeout=15,
                                   what='the turn resumed with the typed answer')
            # The answer resolved the prompt; it never became a turn of its own (no FIFO row).
            rows = authority.db._read_all('SELECT admission_id FROM session_admissions WHERE request_id=?',
                                          ('1700000000.000200',))
            assert rows == []
            assert executed == ['help me get set up']
        finally:
            if 'key' in pending:
                clarify_gateway.clear_session(pending['key'])


@pytest.mark.asyncio
async def test_unrelated_text_with_no_pending_prompt_still_queues_as_a_turn(tmp_path, monkeypatch):
    async with _gateway(tmp_path, 'relay-clarify-none') as (runner, _authority, send, outgoing, _adapter):
        executed = _stub_model(runner, monkeypatch)
        await send('1700000000.000300', 'D3', 'hello')
        await async_wait_until(lambda: _sent(outgoing, 'reply hello'), timeout=20, what='the normal turn')
        assert executed == ['hello']


@pytest.mark.asyncio
async def test_plain_yes_answers_a_blocking_approval_without_stalling_the_relay_reader(tmp_path, monkeypatch):
    """The approval's confirmation is a relay send, whose ``outbound_result`` only the socket reader
    delivers: answered on the reader, every chat on the socket would wait out the 30 s send timeout."""
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry

    async with _gateway(tmp_path, 'relay-approval-typed') as (runner, authority, send, outgoing, _adapter):
        pending = {}

        async def blocked_on_approval(event):
            key = runner._session_key_for_source(event.source)
            entry = _ApprovalEntry({'command': 'rm -rf build', 'description': 'recursive delete'})
            with approval._lock:
                approval._gateway_queues.setdefault(key, []).append(entry)
            pending['key'], pending['entry'] = key, entry
            await asyncio.to_thread(entry.event.wait, 20)
            text = f'approval {entry.result}'
            execution_result.get()['result'] = {'final_response': text, 'messages': []}
            return text

        executed = _stub_model(runner, monkeypatch, {'clean the build': blocked_on_approval})
        thread = '1700000000.000401'
        try:
            await send('1700000000.000400', 'D4', 'clean the build', thread_id=thread)
            await async_wait_until(lambda: 'key' in pending and approval.has_blocking_approval(pending['key']),
                                   timeout=20, what='the turn blocked on approval')
            await send('1700000000.000410', 'D4', 'yes', thread_id=thread)
            # Another chat on the same socket is served while the confirmation is in flight.
            await send('1700000000.000500', 'D5', 'hello')
            await async_wait_until(lambda: _sent(outgoing, 'approval once') and _sent(outgoing, 'reply hello'),
                                   timeout=15, what='the approved turn and the other chat answered')
            rows = authority.db._read_all('SELECT admission_id FROM session_admissions WHERE request_id=?',
                                          ('1700000000.000410',))
            assert rows == []
            assert executed == ['clean the build', 'hello']
        finally:
            if 'key' in pending:
                with approval._lock:
                    approval._gateway_queues.pop(pending['key'], None)


@pytest.mark.asyncio
async def test_a_chat_with_an_admission_in_its_lane_stays_there_once_the_prompt_closes(monkeypatch):
    """Admission moves off the reader while a prompt is pending. Once it closes, the next delivery of a
    chat whose earlier one is still in its lane must queue behind it, never commit ahead inline."""
    from types import SimpleNamespace

    from gateway.config import Platform
    from gateway.relay import inbound_admission

    order, release, prompt = [], asyncio.Event(), {'open': True}

    async def admit(_adapter, _runner, event):
        if event.message_id == 'm1':
            await release.wait()
        order.append(event.message_id)

    monkeypatch.setattr(inbound_admission, '_admit', admit)
    adapter = SimpleNamespace(_admission_lanes={}, _background_tasks=set())
    runner = SimpleNamespace(_hm_any_pending_reply_prompt=lambda: prompt['open'])
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id='C1')

    def deliver(message_id):  # bounded: an inline admission of m1 would block on ``release`` forever
        event = SimpleNamespace(message_id=message_id, source=source)
        return asyncio.wait_for(inbound_admission.admit_relay_delivery(adapter, runner, event), 5)

    first = await deliver('m1')
    assert first is not None  # off the reader
    prompt['open'] = False
    second = await deliver('m2')
    assert second is not None and order == []  # queued behind m1, not committed inline
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert order == ['m1', 'm2']
