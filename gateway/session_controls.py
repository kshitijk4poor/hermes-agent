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
        capabilities = frozenset({'session:read', 'session:submit', 'session:control', 'session:approve'})
        if identity:
            capabilities |= {'session:create'}
        if 'capabilities' in identity:
            capabilities = frozenset(identity['capabilities'])
        if (not identity.get('user_id')
                or identity.get('instance_id', authority.instance_id) != authority.instance_id):
            capabilities = frozenset()
        self.actor = Principal(str(identity.get('user_id') or 'unbound'),
                               identity.get('profile_id', authority.profile_id),
                               capabilities, uuid.uuid4().hex)
        self.subscriptions = {}
        authority.events[self.actor.transport_id] = transport

    async def dispatch(self, request):
        rid = request.get('id')
        method = request.get('method')
        params = request.get('params') or {}
        ref = SessionRef(self.actor.profile_id, params.get('session_id', ''))
        handlers = {'session.create': self.create, 'ping': self.ping, 'runtime.describe': self.describe,
                    'session.list': self.list_sessions, 'session.info': self.info,
                    'session.resume': self.resume, 'prompt.submit': self.submit,
                    'prompt.receipt': self.receipt, 'prompt.cancel': self.cancel,
                    'session.interrupt': self.interrupt, 'session.events.since': self.events_since,
                    'approval.respond': self.respond, 'clarify.respond': self.respond_clarify}
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

    async def create(self, ref, params):
        from gateway.session_local import create_local_session, local_session_info
        ref = create_local_session(self.authority, self.actor, params)
        result = await self.resume(ref, {})
        result['info'] = local_session_info(self.authority, ref)
        return result

    async def ping(self, ref, params):
        if not self.actor.capabilities:
            raise RuntimeStoreError('permission_denied')
        if params:
            raise RuntimeStoreError('invalid_params')
        return {'pong': True}

    async def describe(self, ref, params):
        await self.ping(ref, params)
        return {'instance_id': self.authority.instance_id, 'profile_id': self.authority.profile_id,
                'authority_epoch': self.authority.epoch,
                'capabilities': ['durable-admission-v1', 'event-replay-v1', 'local-cli-create-v1'],
                'session_create': {'sources': ['cli'], 'parameters': ['request_id', 'source']}}

    async def info(self, ref, params):
        from gateway.session_local import local_session_info
        if set(params) != {'session_id'}:
            raise RuntimeStoreError('invalid_params')
        await self.authority.resolve(self.actor, ref)
        return local_session_info(self.authority, ref)

    async def list_sessions(self, ref, params):
        if 'session:read' not in self.actor.capabilities:
            raise RuntimeStoreError('permission_denied')
        limit = params.get('limit', 200)
        if set(params) - {'limit'} or type(limit) is not int or not 1 <= limit <= 200:
            raise RuntimeStoreError('invalid_params')
        sessions = []
        for sid in tuple(self.authority.sessions):
            candidate = SessionRef(self.actor.profile_id, sid)
            try:
                handle = await self.authority.resolve(self.actor, candidate)
            except RuntimeStoreError as exc:
                if exc.reason not in {'permission_denied', 'profile_mismatch', 'not_found'}:
                    raise
                continue
            row = self.authority.db.get_session(sid)
            sessions.append({'session_id': sid, 'id': sid, 'title': row.get('title') or '',
                             'source': row.get('source'), 'started_at': row.get('started_at'),
                             'message_count': row.get('message_count', 0),
                             'running': handle.execution_state == 'running'})
        sessions.sort(key=lambda row: row['started_at'] or 0, reverse=True)
        return {'sessions': sessions[:limit], 'scope': 'live'}

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

    async def respond_clarify(self, ref, params):
        if set(params) != {"session_id", "execution_generation", "prompt_id", "answer"}:
            raise RuntimeStoreError("invalid_params")
        return await self.authority.respond(self.actor, ref, params["execution_generation"],
            params["prompt_id"], {"answer": params["answer"]}, kind="clarify")

    async def close(self):
        for subscription in self.subscriptions.values():
            await self.authority.detach(self.actor, subscription)
        self.subscriptions.clear()
        self.authority.events.pop(self.actor.transport_id, None)
