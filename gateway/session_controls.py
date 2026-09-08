"""Authenticated WS projection of the gateway authority; no TUI execution fallback."""
from dataclasses import asdict
import sqlite3
import uuid

from gateway.session_contract import Principal, SessionRef, Submission
from hermes_state_runtime import RuntimeStoreError


class AuthorityConnection:
    def __init__(self, authority, transport, identity):
        self.authority = authority
        self.transport = transport
        self.actor = Principal(str(identity.get('user_id') or 'authenticated-dashboard'),
                               authority.profile_id,
                               frozenset({'session:read', 'session:submit', 'session:control', 'session:approve'}),
                               uuid.uuid4().hex)
        self.subscriptions = {}
        authority.events[self.actor.transport_id] = transport

    async def dispatch(self, request):
        rid = request.get('id')
        method = request.get('method')
        params = request.get('params') or {}
        ref = SessionRef(self.actor.profile_id, params.get('session_id', ''))
        handlers = {'session.resume': self.resume, 'prompt.submit': self.submit,
                    'prompt.receipt': self.receipt, 'prompt.cancel': self.cancel,
                    'session.interrupt': self.interrupt, 'session.events.since': self.events_since,
                    'approval.respond': self.respond}
        try:
            if method not in handlers:
                raise RuntimeStoreError('invalid_params')
            result = await handlers[method](ref, params)
            return {'jsonrpc': '2.0', 'id': rid, 'result': result}
        except RuntimeStoreError as exc:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {
                'code': 4001, 'message': exc.reason, 'data': {'reason': exc.reason}}}
        except sqlite3.Error:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {
                'code': 5001, 'message': 'storage_unavailable', 'data': {'reason': 'storage_unavailable'}}}

    async def resume(self, ref, params):
        snapshot = await self.authority.attach(self.actor, ref)
        self.subscriptions[ref.session_id] = snapshot.subscription_id
        return {'session_id': ref.session_id, 'stored_session_id': ref.session_id,
                'messages': list(snapshot.history), 'message_count': len(snapshot.history),
                'running': snapshot.handle.execution_state == 'running',
                'authority_epoch': snapshot.handle.authority_epoch,
                'replay_epoch': snapshot.replay_epoch, 'last_sequence': snapshot.last_sequence,
                'subscription_id': snapshot.subscription_id, 'revision': snapshot.handle.revision,
                'execution_generation': snapshot.handle.execution_generation,
                'pending': [asdict(r) for r in snapshot.pending],
                'prompts': list(snapshot.prompts), 'info': {}}

    async def events_since(self, ref, params):
        self.authority.authorize(self.actor, ref, 'session:read')
        sequence = params.get('last_sequence', params.get('last_seen', 0))
        epoch = params.get('replay_epoch')
        if type(sequence) is not int or sequence < 0 or (epoch is not None and not isinstance(epoch, str)):
            raise RuntimeStoreError('invalid_params')
        return self.authority.sessions[ref.session_id].event_stream.since(epoch, sequence)

    async def submit(self, ref, params):
        if ref.session_id not in self.subscriptions:
            raise RuntimeStoreError('permission_denied')
        forbidden = set(params) - {'session_id', 'text', 'submission_id', 'input_id', 'queued'}
        if forbidden:
            raise RuntimeStoreError('invalid_params')
        request_id = params.get('submission_id') or params.get('input_id')
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeStoreError('invalid_params')
        receipt = await self.authority.submit(self.actor, Submission(
            request_id, ref, {'text': params.get('text')}, 'queue'))
        return asdict(receipt)

    async def receipt(self, ref, params):
        return asdict(await self.authority.receipt(self.actor, ref, params.get('admission_id')))

    async def cancel(self, ref, params):
        return asdict(await self.authority.cancel_queued(self.actor, ref, params.get('admission_id')))

    async def interrupt(self, ref, params):
        return asdict(await self.authority.interrupt(self.actor, ref, params.get('execution_generation')))

    async def respond(self, ref, params):
        if set(params) != {"session_id", "execution_generation", "prompt_id", "choice"}:
            raise RuntimeStoreError("invalid_params")
        return await self.authority.respond(self.actor, ref, params["execution_generation"],
                                            params["prompt_id"], {"choice": params["choice"]})

    async def close(self):
        for subscription in self.subscriptions.values():
            await self.authority.detach(self.actor, subscription)
        self.subscriptions.clear()
        self.authority.events.pop(self.actor.transport_id, None)
