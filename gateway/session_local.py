"""Fresh local routes owned by the authenticated gateway, not by a viewer.

Source and supported launch settings are frozen per route, never process env.
"""
from __future__ import annotations

import uuid

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource
from hermes_state_runtime import RuntimeStoreError


class LocalSessionAdapter(BasePlatformAdapter):
    """Delivery stays in the canonical event stream; no second local transport."""

    def __init__(self, authority):
        super().__init__(PlatformConfig(enabled=True), Platform.LOCAL)
        self.authority = authority
        self.sources = {}
        self.policies = {}

    def authorize_source(self, source):
        saved = self.sources.get(source.chat_id)
        if saved is None:
            return False
        original, identity = saved
        return (source is original and self._identity(source) == identity
                and any(live.source is source for live in self.authority.sessions.values()))

    @staticmethod
    def _identity(source):
        return source.platform, source.chat_id, source.user_id, source.profile, source.chat_type

    def register_source(self, source):
        self.sources[source.chat_id] = source, self._identity(source)

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        self.sources.clear()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # TurnRunner already publishes model/tool deltas and settlement exactly once.
        return SendResult(success=True, message_id=uuid.uuid4().hex)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {'id': chat_id, 'type': 'dm'}

    async def send_exec_approval(self, **kwargs):
        # The existing callback owns registration in PendingControls. An ACK here
        # selects that rendering path without a plaintext fallback or a second waiter.
        return SendResult(success=True, message_id=uuid.uuid4().hex)


def authorize_local_source(runner, source):
    """None for legacy/nonlocal routes; registered local adapters fail closed."""
    if source.platform != Platform.LOCAL:
        return None
    # A forged profile must not miss this adapter and fall into a messaging
    # allow-all policy. Local source identity is owned by this authority alone.
    adapter = runner._primary_adapters().get(Platform.LOCAL)
    if not isinstance(adapter, LocalSessionAdapter):
        return None
    if adapter.authority is not getattr(runner, 'session_authority', None):
        return False
    return adapter.authorize_source(source)


def create_local_session(authority, actor, params):
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if 'session:create' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    from gateway.session_policy import build_policy
    from gateway.run import _load_gateway_config, _resolve_gateway_model
    from dataclasses import replace
    policy = build_policy(params, _load_gateway_config())
    if policy.model is None:
        policy = replace(policy, model=_resolve_gateway_model(policy.config()))
    request_id = params.get('request_id', uuid.uuid4().hex)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        raise RuntimeStoreError('invalid_params')
    from dataclasses import asdict
    from datetime import datetime, timezone
    from gateway.session import SessionEntry
    from gateway.session_local_recovery import local_identity, restore_local_session
    from hermes_state_local import commit_local_session
    authority._require_admission_open()
    sid = local_identity(authority.profile_id, actor.subject, request_id)
    from gateway.session_policy import bind_launch_key
    policy = bind_launch_key(authority, sid, policy, params.get("api_key"))
    source = SessionSource(platform=Platform.LOCAL, chat_id=sid,
                           user_id=actor.subject, chat_type='dm')
    route = authority.runner.session_store._generate_session_key(source)
    now = datetime.now(timezone.utc)
    entry = SessionEntry(route, sid, now, now, origin=source, platform=Platform.LOCAL)
    commit_local_session(authority.db, epoch=authority.epoch, receipt={
        'profile_id': authority.profile_id, 'principal_id': actor.subject, 'request_id': request_id,
        'session_id': sid, 'route': route, 'entry': entry.to_dict(), 'policy': asdict(policy)})
    return restore_local_session(authority, sid)


def local_session_info(authority, ref):
    live = authority.sessions[ref.session_id]
    agent = authority.agent(ref)
    from gateway.session_policy import policy_for_source
    policy = policy_for_source(authority.runner, live.source)
    return {'source': policy.source if policy else live.source.platform.value,
            'model': getattr(agent, 'model', policy.model if policy else None), 'lazy': agent is None,
            'profile_id': authority.profile_id}
