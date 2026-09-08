"""Gateway-owned live sessions and canonical durable FIFO scheduling.

Only the runtime bootstrap holding profile ownership may initialize this service.
Transport attachment never constructs an agent or takes a turn lease.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import uuid

from gateway.session_contract import (
    AdmissionReceipt, Principal, SessionHandle, SessionRef, Submission,
    SubscriptionSnapshot,
)
from gateway.session_events import SessionEvents
from gateway.session_pending_controls import PendingApprovals
from hermes_state_runtime import (
    RuntimeStoreError, admit_session_input, begin_runtime_epoch,
    cancel_session_input, claim_session_input, get_session_admission,
    list_session_admissions, recover_session_inputs, settle_session_input,
)


@dataclass
class LiveSession:
    source: object
    route: str
    task: asyncio.Task | None = None
    subscribers: dict = field(default_factory=dict)
    event_stream: SessionEvents = field(default_factory=SessionEvents)
    controls: PendingApprovals = field(init=False)

    def __post_init__(self):
        self.controls = PendingApprovals(self.event_stream)


class SessionAuthority:
    def __init__(self, runner, *, profile_id, instance_id, db, epoch):
        self.runner = runner
        self.profile_id = profile_id
        self.instance_id = instance_id
        self.db = db
        self.epoch = epoch
        self.sessions = {}
        self.waiters = {}
        self.events = {}
        self.native_waiters = set()

    def authorize(self, actor, ref, capability):
        if actor.profile_id != self.profile_id or ref.profile_id != self.profile_id:
            raise RuntimeStoreError('profile_mismatch')
        if capability not in actor.capabilities:
            raise RuntimeStoreError('permission_denied')
        if ref.session_id not in self.sessions:
            raise RuntimeStoreError('not_found')

    def register(self, source):
        entry = self.runner.session_store.get_or_create_session(source)
        sid = entry.session_id
        self.sessions.setdefault(sid, LiveSession(source, entry.session_key))
        # SessionStore reserves routing metadata before the first AIAgent exists.
        if self.db.get_session(sid) is None:
            self.db.create_session(sid, source=source.platform.value)
        return SessionRef(self.profile_id, sid)

    def agent(self, ref):
        return self.runner._cached_agent_for(self.sessions[ref.session_id].route)

    def _handle(self, ref):
        row = self.db.get_session(ref.session_id)
        pending = list_session_admissions(self.db, session_id=ref.session_id)
        state = 'unknown' if any(r['status'] == 'unknown' for r in pending) else (
            'running' if any(r['status'] == 'started' for r in pending) else 'idle')
        return SessionHandle(ref, self.instance_id, self.epoch, row['runtime_revision'],
                             row['runtime_generation'], state)

    async def resolve(self, actor, ref):
        self.authorize(actor, ref, 'session:read')
        return self._handle(ref)

    async def attach(self, actor, ref):
        self.authorize(actor, ref, 'session:read')
        live = self.sessions[ref.session_id]
        with live.event_stream.lock:
            subscription = next((key for key, member in live.subscribers.items()
                                 if member == actor), None) or uuid.uuid4().hex
            live.subscribers[subscription] = actor
            transport = self.events.get(actor.transport_id)
            if transport is not None:
                live.event_stream.fanout.attach(transport)
            handle = self._handle(ref)
            prompts = live.controls.snapshot(ref.session_id, handle.execution_generation)
            epoch, sequence = live.event_stream.watermark()
            return SubscriptionSnapshot(subscription, handle, epoch,
                                        sequence, tuple(self.db.get_messages_as_conversation(ref.session_id)),
                                        tuple(self._receipt(r) for r in list_session_admissions(
                                            self.db, session_id=ref.session_id)), prompts)

    async def detach(self, actor, subscription_id):
        for live in self.sessions.values():
            if subscription_id in live.subscribers:
                if live.subscribers[subscription_id] != actor:
                    raise RuntimeStoreError('permission_denied')
                del live.subscribers[subscription_id]
                transport = self.events.get(actor.transport_id)
                if transport is not None:
                    live.event_stream.fanout.detach(transport)
                return
        raise RuntimeStoreError('not_found')

    def _receipt(self, row):
        return AdmissionReceipt(row['admission_id'], SessionRef(self.profile_id, row['target_session_id']),
                                row['seq'], row['status'], row['outcome'],
                                row['owner_epoch'] or self.epoch, row['generation'])

    def _schedule(self, ref):
        live = self.sessions[ref.session_id]
        if live.task is None or live.task.done():
            live.task = asyncio.create_task(self._drain(ref))

    def admit_native(self, event):
        """Trusted adapter entry; commit the snapshot before yielding or ACKing."""
        import json
        from gateway.session_envelope import snapshot_native, restore_native
        payload = snapshot_native(self.runner, event)
        source = restore_native(payload).source
        ref = self.register(source)
        identity = json.dumps([source.profile, source.platform.value, source.chat_id,
                               source.thread_id, source.user_id], separators=(',', ':'))
        row = admit_session_input(self.db, epoch=self.epoch, principal_id='messaging:' + identity,
                                  session_id=ref.session_id,
                                  request_id=str(event.message_id or uuid.uuid4().hex), payload=payload)
        event._gateway_accepted = True
        self._schedule(ref)
        return self._receipt(row)

    async def recover_native_sessions(self, bindings):
        """Bind only server-observed native routes; unknown work stays paused."""
        from collections import Counter
        from gateway.session_envelope import check_native_route
        bindings = list(bindings)
        counts = Counter(sid for sid, _, _ in bindings)
        results = {}
        for sid, available_source, adapter in bindings:
            try:
                if counts[sid] != 1:
                    raise RuntimeStoreError('admission_conflict')
                rows = list_session_admissions(self.db, session_id=sid, pending_only=False)
                native = [row for row in rows if 'native_text_v1' in row['payload']]
                if not native:
                    raise RuntimeStoreError('not_found')
                source, route = check_native_route(self.runner, native[-1]['payload'], sid,
                                                    available_source, adapter)
                for row in rows:
                    if row['status'] == 'queued':
                        if 'native_text_v1' not in row['payload']:
                            raise RuntimeStoreError('invalid_params')
                        check_native_route(self.runner, row['payload'], sid, available_source, adapter)
                self.sessions.setdefault(sid, LiveSession(source, route))
                if any(row['status'] == 'unknown' for row in rows):
                    raise RuntimeStoreError('unknown_execution')
                self._schedule(SessionRef(self.profile_id, sid))
                results[sid] = 'ready'
            except RuntimeStoreError as exc:
                results[sid] = exc.reason
        return results

    async def submit(self, actor: Principal, request: Submission):
        self.authorize(actor, request.ref, 'session:submit')
        if request.intent != 'queue' or set(request.payload) != {'text'} or not isinstance(request.payload['text'], str):
            raise RuntimeStoreError('invalid_params')
        row = admit_session_input(self.db, epoch=self.epoch, principal_id=actor.subject,
                                  session_id=request.ref.session_id, request_id=request.request_id,
                                  payload=dict(request.payload), intent=request.intent)
        live = self.sessions[request.ref.session_id]
        if live.task is None or live.task.done():
            live.task = asyncio.create_task(self._drain(request.ref))
        return self._receipt(row)

    async def receipt(self, actor, ref, admission_id):
        self.authorize(actor, ref, 'session:submit')
        row = get_session_admission(self.db, admission_id=admission_id)
        if row is None or row['target_session_id'] != ref.session_id:
            raise RuntimeStoreError('not_found')
        if row['principal_id'] != actor.subject and 'session:control' not in actor.capabilities:
            raise RuntimeStoreError('permission_denied')
        return self._receipt(row)

    async def cancel_queued(self, actor, ref, admission_id):
        await self.receipt(actor, ref, admission_id)
        return self._receipt(cancel_session_input(self.db, epoch=self.epoch, admission_id=admission_id))

    async def interrupt(self, actor, ref, generation):
        self.authorize(actor, ref, 'session:control')
        handle = self._handle(ref)
        if handle.execution_generation != generation:
            raise RuntimeStoreError('stale_generation')
        agent = self.agent(ref)
        if agent is not None and handle.execution_state == 'running':
            agent.interrupt()
        return self._handle(ref)

    async def respond(self, actor, ref, generation, prompt_id, response):
        self.authorize(actor, ref, "session:approve")
        live = self.sessions[ref.session_id]
        with live.event_stream.lock:
            if actor not in live.subscribers.values():
                raise RuntimeStoreError("permission_denied")
            if type(generation) is not int or generation != self._handle(ref).execution_generation:
                raise RuntimeStoreError("stale_generation")
            if not isinstance(prompt_id, str) or not prompt_id:
                raise RuntimeStoreError("invalid_params")
            return live.controls.respond(ref.session_id, generation, prompt_id, response)

    async def _drain(self, ref):
        from gateway.session_ingress import execute_admission
        live = self.sessions[ref.session_id]
        while True:
            try:
                pending = list_session_admissions(self.db, session_id=ref.session_id)
                if any(row['status'] == 'unknown' for row in pending):
                    return
                first = next((row for row in pending if row['status'] == 'queued'), None)
                if first is not None and 'native_text_v1' in first['payload']:
                    from gateway.session_envelope import check_native_route
                    check_native_route(self.runner, first['payload'], ref.session_id, live.source,
                                       self.runner._adapter_for_source(live.source))
                row = claim_session_input(self.db, epoch=self.epoch, session_id=ref.session_id)
            except RuntimeStoreError as exc:
                import logging
                logging.getLogger(__name__).warning('Session %s paused: %s', ref.session_id, exc.reason)
                return
            if row is None:
                return
            admission_id = row['admission_id']
            try:
                response = await execute_admission(self, ref, row)
                outcome = 'completed'
            except Exception:
                response = 'The admitted turn failed.'
                outcome = 'failed'
            settled = settle_session_input(self.db, epoch=self.epoch, admission_id=admission_id,
                                           generation=row['generation'], outcome=outcome)
            live.controls.snapshot(ref.session_id, row['generation'])
            live.event_stream.publish(ref.session_id, {
                'text': response, 'content': response, 'admission_id': admission_id,
                'outcome': settled['outcome']})
            waiter = self.waiters.pop(admission_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(response)


async def initialize_session_authority(runner, *, profile_id, instance_id):
    """Call after exclusive profile ownership, before connecting adapters/API."""
    db = getattr(runner._session_db, '_db', runner._session_db)
    epoch = begin_runtime_epoch(db, instance_id=instance_id)
    recover_session_inputs(db, epoch=epoch)
    authority = SessionAuthority(runner, profile_id=profile_id, instance_id=instance_id, db=db, epoch=epoch)
    runner.session_authority = authority
    return authority
