"""Independent review probes, not production changes."""
import asyncio

import pytest

from gateway.session_results import execution_result
from tests.gateway.relay.test_relay_native_turn_lifecycle import _gateway, _stub_model
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True], ids=['overlapping-retry', 'cancelled-original'])
async def test_cancel_after_approval_resolution_does_not_allow_replay_to_approve_next(tmp_path, monkeypatch, cancel):
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry

    async with _gateway(tmp_path, 'review-cancel') as (runner, authority, send, outgoing, adapter):
        pending, answers = {}, []
        sending = asyncio.Event()
        release = asyncio.Event()
        original_send = runner._send_busy_reply

        async def held_confirmation(*args, **kwargs):
            sending.set()
            await release.wait()
            return await original_send(*args, **kwargs)

        monkeypatch.setattr(runner, '_send_busy_reply', held_confirmation)

        async def ask_twice(event):
            key = runner._session_key_for_source(event.source)
            pending['key'] = key
            for number in (1, 2):
                entry = _ApprovalEntry({'command': f'rm -rf build{number}', 'description': 'recursive delete'})
                with approval._lock:
                    approval._gateway_queues.setdefault(key, []).append(entry)
                pending['number'], pending['entry'] = number, entry
                await asyncio.to_thread(entry.event.wait, 20)
                answers.append(entry.result)
            execution_result.get()['result'] = {'final_response': 'finished', 'messages': []}
            return 'finished'

        _stub_model(runner, monkeypatch, {'clean': ask_twice})
        thread = '1700000000.000001'
        message_id = '1700000000.000110'
        try:
            await send('1700000000.000100', 'D1', 'clean', thread_id=thread)
            await async_wait_until(lambda: pending.get('number') == 1, timeout=20, what='first approval')
            await send(message_id, 'D1', 'yes', thread_id=thread)
            await asyncio.wait_for(sending.wait(), 10)
            await async_wait_until(lambda: pending.get('number') == 2, timeout=10, what='second approval')
            task = adapter._admission_lanes[('slack', 'D1')]
            if cancel:
                await adapter.cancel_background_tasks()
                assert task.cancelled()
                assert adapter._admission_lanes == {}
            assert not any(f.get('type') == 'inbound_ack' and f.get('bufferId') == 'buf-' + message_id for f in outgoing)
            await send(message_id, 'D1', 'yes', thread_id=thread)
            if not cancel:
                await async_wait_until(lambda: adapter._admission_lanes.get(('slack', 'D1')) is not task,
                                       timeout=10, what='overlapping resend queued in lane')
            release.set()
            expected_acks = 1 if cancel else 2
            await async_wait_until(lambda: sum(f.get('type') == 'inbound_ack' and f.get('bufferId') == 'buf-' + message_id for f in outgoing) == expected_acks, timeout=10, what='retry ACK')
            print('REVIEW evidence: second approval resolved by replay =', pending['entry'].event.is_set(), 'answers =', answers,
                  'answered receipts =', getattr(authority, '_answered_deliveries', {}))
            assert not pending['entry'].event.is_set(), 'replayed first yes approved the second dangerous command'
        finally:
            release.set()
            if 'key' in pending:
                approval.resolve_gateway_approval(pending['key'], 'deny', resolve_all=True)
