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


@pytest.mark.asyncio
async def test_a_re_sent_answer_never_answers_the_next_prompt(tmp_path, monkeypatch):
    """The connector re-sends an unacknowledged frame. A consumed answer leaves no FIFO row to resolve
    the re-send to, so without a receipt it would answer whatever prompt comes next."""
    from tools import clarify_gateway

    async with _gateway(tmp_path, 'relay-clarify-resend') as (runner, _authority, send, outgoing, _adapter):
        pending, answers = {}, []

        async def ask_twice(event):
            key = runner._session_key_for_source(event.source)
            pending['key'] = key
            for number in (1, 2):
                entry = clarify_gateway.register(f'resend-q{number}', key, f'Question {number}?', None)
                pending['number'] = number
                answers.append(await asyncio.to_thread(clarify_gateway.wait_for_response, entry.clarify_id, 20))
            execution_result.get()['result'] = {'final_response': 'both answered', 'messages': []}
            return 'both answered'

        def acks(message_id):
            return sum(f.get('type') == 'inbound_ack' and f.get('bufferId') == 'buf-' + message_id for f in outgoing)

        _stub_model(runner, monkeypatch, {'ask twice': ask_twice})
        thread = '1700000000.000601'
        try:
            await send('1700000000.000600', 'D6', 'ask twice', thread_id=thread)
            await async_wait_until(lambda: pending.get('number') == 1, timeout=20, what='the first question')
            await send('1700000000.000610', 'D6', 'first answer', thread_id=thread)
            await async_wait_until(lambda: pending.get('number') == 2, timeout=20, what='the second question')
            await send('1700000000.000610', 'D6', 'first answer', thread_id=thread)  # the connector re-sends it
            await async_wait_until(lambda: acks('1700000000.000610') == 2, timeout=20, what='the re-send acked')
            assert clarify_gateway.get_pending('resend-q2') is not None  # still waiting for a real answer
            await send('1700000000.000620', 'D6', 'second answer', thread_id=thread)
            await async_wait_until(lambda: _sent(outgoing, 'both answered'), timeout=20, what='the turn finished')
            assert answers == ['first answer', 'second answer']
        finally:
            if 'key' in pending:
                clarify_gateway.clear_session(pending['key'])


@pytest.mark.asyncio
async def test_a_reader_admitted_delivery_never_answers_a_prompt_that_opened_after_the_check(
        tmp_path, monkeypatch):
    """The reader checks for pending prompts before routing; a prompt can open after that check, and
    a hook present only in the routed profile still selects the prepared path. Answering there would
    send from the reader, so the reader's mark must hold at the answer itself."""
    from contextlib import asynccontextmanager

    from gateway.platforms import webhook_ingress
    from gateway.session_ingress_context import _callback
    from hermes_cli import lifecycle
    from tools import slash_confirm

    async with _gateway(tmp_path, 'relay-reader-mark') as (runner, _authority, send, outgoing, _adapter):
        observed = {}
        # A pre_gateway_dispatch hook only inside the routed profile's scope, not the reader's.
        monkeypatch.setattr(lifecycle, 'has_hook', lambda name: name == 'pre_gateway_dispatch'
                            and _callback.get() is not None)

        async def passthrough(event, _source):
            return event

        monkeypatch.setattr(runner, '_hm_pre_gateway_dispatch_hook', passthrough)
        original_scope = webhook_ingress.producer_scope

        @asynccontextmanager
        async def prompt_opens_inside(adapter, event):
            async with original_scope(adapter, event) as authority:
                observed['reader_marked'] = getattr(event, '_admitted_on_reader', False)
                key = runner._session_key_for_source(event.source)

                async def confirmed(choice):
                    observed['choice'] = choice

                slash_confirm.register(key, 'reader-prompt', 'reload-mcp', confirmed)
                try:
                    yield authority
                finally:
                    slash_confirm.clear(key)

        monkeypatch.setattr(webhook_ingress, 'producer_scope', prompt_opens_inside)
        _stub_model(runner, monkeypatch)
        await send('1700000000.000700', 'D7', 'yes')
        await async_wait_until(lambda: _sent(outgoing, 'reply yes'), timeout=20, what='queued as a normal turn')
        assert observed['reader_marked']
        assert 'choice' not in observed
