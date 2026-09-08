"""Trusted messaging admission and the existing TurnRunner invocation boundary."""
import asyncio
from contextvars import ContextVar
import json
import uuid

from gateway.platforms.event import MessageEvent
from gateway.session_contract import Principal, Submission

executing_admission = ContextVar('executing_admission', default=False)


async def admit_message(authority, event):
    ref = authority.register(event.source)
    source = event.source
    identity = json.dumps([source.profile, source.platform.value, source.chat_id,
                           source.thread_id, source.user_id], separators=(',', ':'))
    actor = Principal('messaging:' + identity, authority.profile_id,
                      frozenset({'session:submit'}), '')
    request_id = str(event.message_id or uuid.uuid4().hex)
    receipt = await authority.submit(actor, Submission(request_id, ref, {'text': event.text}, 'queue'))
    if receipt.status == 'terminal':
        return None
    # These are execution envelopes, not a second queue. The durable ledger alone orders claims.
    authority.native_events[receipt.admission_id] = event
    waiter = authority.waiters.setdefault(receipt.admission_id, asyncio.get_running_loop().create_future())
    return await asyncio.shield(waiter)


async def execute_admission(authority, ref, row):
    live = authority.sessions[ref.session_id]
    event = authority.native_events.pop(row['admission_id'], None)
    native = event is not None
    if event is None:
        event = MessageEvent(text=row['payload']['text'], source=live.source,
                             message_id=row['admission_id'])
    token = executing_admission.set(True)
    try:
        response = await authority.runner._handle_message(event)
        if not native and response:
            adapter = authority.runner._adapter_for_source(event.source)
            if adapter is not None:
                await adapter.send(event.source.chat_id, response,
                                   metadata={'thread_id': event.source.thread_id})
        return response
    finally:
        executing_admission.reset(token)
