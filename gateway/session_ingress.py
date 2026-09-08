"""Trusted messaging admission and the existing TurnRunner invocation boundary."""
import asyncio
from contextvars import ContextVar
from contextlib import nullcontext
from dataclasses import replace

from gateway.platforms.event import MessageEvent
from gateway.session_envelope import restore_native

executing_admission = ContextVar('executing_admission', default=False)


async def admit_message(authority, event):
    receipt = await authority.admit_native(event)
    if receipt.status == 'terminal':
        return None
    # Only the delivery waiter is process-local; execution reads the committed snapshot.
    authority.native_waiters.add(receipt.admission_id)
    waiter = authority.waiters.setdefault(receipt.admission_id, asyncio.get_running_loop().create_future())
    return await asyncio.shield(waiter)


async def execute_admission(authority, ref, row):
    live = authority.sessions[ref.session_id]
    native = row['admission_id'] in authority.native_waiters
    authority.native_waiters.discard(row['admission_id'])
    if 'native_text_v1' in row['payload']:
        event = restore_native(row['payload'], authority.runner)
    else:
        event = MessageEvent(text=row['payload']['text'], source=live.source,
                             message_id=row['admission_id'])
        if 'local_automation_v1' in row['payload']:
            from gateway.session_automation import restore_local_automation
            event = restore_local_automation(authority, ref, row)
    provenance = row['payload'].get('native_text_v1', {}).get('provenance')
    scope = nullcontext()
    if provenance is not None:
        from gateway.run import _profile_runtime_scope
        from gateway.session_ingress_context import restore_provenance
        home = restore_provenance(authority.runner, event.source, provenance)
        scope = _profile_runtime_scope(home)
    token = executing_admission.set(True)
    try:
        with scope:
            response = await authority.runner._handle_message(event)
            if not native and response:
                adapter = authority.runner._adapter_for_source(event.source)
                if adapter is not None:
                    await deliver_response(adapter, event, live.route, response)
            return response
    finally:
        executing_admission.reset(token)


async def deliver_response(adapter, event, session_key, response):
    from gateway.platforms.base import _thread_metadata_for_event, _mark_notify_metadata
    text, ttl = adapter._unwrap_ephemeral(response)
    if not text:
        return
    extracted = await adapter._extract_response_content(text, event, session_key, is_ephemeral_response=ttl > 0)
    metadata = _mark_notify_metadata(_thread_metadata_for_event(event))
    results = []
    if extracted.text_content:
        await adapter._send_final_text(event, session_key, extracted.text_content,
                                       metadata, ttl > 0, ttl, results.append)
    await adapter._deliver_attachments(event, extracted, metadata, anything_sent=bool(results))


async def dispatch_shared_busy(adapter, event, session_key):
    delivery_event = replace(event, source=replace(event.source))
    response = await adapter._message_handler(event)
    await deliver_response(adapter, delivery_event, session_key, response)
