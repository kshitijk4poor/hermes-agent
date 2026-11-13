"""Independent review probes; isolated copy only."""
import asyncio
import pytest

from gateway.session_results import execution_result
from tests.gateway.relay.test_relay_native_turn_lifecycle import _actions, _gateway, _stub_model
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_replayed_typed_answer_cannot_answer_next_prompt(tmp_path, monkeypatch):
    from tools import clarify_gateway
    async with _gateway(tmp_path, 'review-replay') as (runner, authority, send, outgoing, adapter):
        pending = {}
        answers = []
        async def ask(event):
            key = runner._session_key_for_source(event.source)
            pending['key'] = key
            for number in (1, 2):
                entry = clarify_gateway.register(f'review-q{number}', key, f'Question {number}?', None)
                pending['number'] = number
                answers.append(await asyncio.to_thread(clarify_gateway.wait_for_response, entry.clarify_id, 20))
            text = 'review completed'
            execution_result.get()['result'] = {'final_response': text, 'messages': []}
            return text
        _stub_model(runner, monkeypatch, {'ask': ask})
        def ack_count(mid):
            return sum(frame.get('type') == 'inbound_ack' and frame.get('bufferId') == 'buf-' + mid for frame in outgoing)
        try:
            await send('1700000000.001000', 'D1', 'ask', thread_id='1700000000.001000')
            await async_wait_until(lambda: pending.get('number') == 1, timeout=20)
            await send('1700000000.001001', 'D1', 'first answer', thread_id='1700000000.001000')
            await async_wait_until(lambda: pending.get('number') == 2, timeout=20)
            await send('1700000000.001001', 'D1', 'first answer', thread_id='1700000000.001000')
            await async_wait_until(lambda: ack_count('1700000000.001001') == 2, timeout=20)
            second = clarify_gateway.get_pending('review-q2')
            replay_answer = None if second is not None else (answers[1] if len(answers) > 1 else 'resolved')
            print('REPLAY', {'answers': answers, 'second_pending': second is not None,
                             'rows': authority.db._read_all('SELECT admission_id FROM session_admissions WHERE request_id=?', ('1700000000.001001',))})
            clarify_gateway.clear_session(pending['key'])
            await async_wait_until(lambda: any(a.get('content') == 'review completed' for a in _actions(outgoing)), timeout=20)
            assert replay_answer is None, 'The replay answered the next prompt: ' + str(replay_answer)
        finally:
            if 'key' in pending:
                clarify_gateway.clear_session(pending['key'])


@pytest.mark.asyncio
async def test_reader_marker_survives_scoped_hook_discovery(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from gateway.platforms import webhook_ingress
    from gateway.session_ingress_context import _callback
    from hermes_cli import lifecycle
    from tools import slash_confirm
    async with _gateway(tmp_path, 'review-reader') as (runner, authority, send, outgoing, adapter):
        observed = {}
        adapter._transport._outbound_timeout_s = 2
        original_scope = webhook_ingress.producer_scope
        # A routed profile can have a hook that is absent from the reader's launch profile.
        monkeypatch.setattr(lifecycle, 'has_hook', lambda name: name == 'pre_gateway_dispatch' and _callback.get() is not None)
        async def noop(event, source):
            return event
        monkeypatch.setattr(runner, '_hm_pre_gateway_dispatch_hook', noop)
        @asynccontextmanager
        async def opening_scope(adapter, event):
            async with original_scope(adapter, event) as authority:
                observed['reader_marked'] = getattr(event, '_admitted_on_reader', False)
                observed['admission_task'] = asyncio.current_task()
                key = runner._session_key_for_source(event.source)
                # A worker opening a prompt between the reader check and prepared admission.
                async def confirmed(choice):
                    observed['handler_task'] = asyncio.current_task()
                    observed['choice'] = choice
                    result = await adapter.send(event.source.chat_id, 'reader confirmation')
                    observed['send_result'] = result.raw_response
                    return None
                slash_confirm.register(key, 'review-reader-prompt', 'reload-mcp', confirmed)
                try:
                    yield authority
                finally:
                    slash_confirm.clear(key)
        monkeypatch.setattr(webhook_ingress, 'producer_scope', opening_scope)
        await send('1700000000.002000', 'D2', 'yes')
        await async_wait_until(lambda: any(f.get('type') == 'inbound_ack' and f.get('bufferId') == 'buf-1700000000.002000' for f in outgoing), timeout=20)
        same_task = observed.get('handler_task') is observed['admission_task']
        print('READER', {'reader_marked': observed['reader_marked'], 'handler_on_reader': same_task, 'choice': observed.get('choice')})
        assert observed['reader_marked']
        assert 'choice' not in observed, 'Reader-marked delivery ran the prompt handler inline'
