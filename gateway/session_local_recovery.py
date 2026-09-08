"""Restore only private, profile-bound local policy into the existing authority."""
import hashlib
import json
import logging

from gateway.config import Platform
from gateway.session import SessionEntry, SessionSource
from gateway.session_contract import SessionRef
from hermes_state_local import POLICY_PREFIX, local_receipt
from hermes_state_runtime import RuntimeStoreError, list_session_admissions


def local_identity(profile_id, principal_id, request_id):
    identity = json.dumps([profile_id, principal_id, request_id], separators=(',', ':'))
    return 'local-' + hashlib.sha256(identity.encode()).hexdigest()


def restore_local_session(authority, sid):
    from gateway.session_authority import LiveSession
    from gateway.session_local import LocalSessionAdapter
    from gateway.session_policy import restore_policy
    receipt = local_receipt(authority.db, sid)
    try:
        if receipt['profile_id'] != authority.profile_id:
            raise RuntimeStoreError('profile_mismatch')
        chat_id = local_identity(receipt['profile_id'], receipt['principal_id'], receipt['request_id'])
        if receipt['session_id'] != sid or sid != chat_id:
            raise ValueError('identity mismatch')
        source = SessionSource(platform=Platform.LOCAL, chat_id=chat_id,
                               user_id=receipt['principal_id'], chat_type='dm')
        store = authority.runner.session_store
        route = store._generate_session_key(source)
        if receipt['route'] != route:
            raise ValueError('route mismatch')
        policy = restore_policy(receipt['policy'])
        entry = SessionEntry.from_dict(receipt['entry'])
        if (entry.session_id != sid or entry.session_key != route
                or entry.origin.to_dict() != source.to_dict()):
            raise ValueError('entry mismatch')
        row = authority.db.get_session(sid)
        if row is None or (row['session_key'], row['chat_id'], row['user_id']) != (route, chat_id, source.user_id):
            raise ValueError('stored identity mismatch')
        if authority.db.get_compression_chain(sid)[-1] != sid:
            # A creation receipt cannot authorize a different execution target.
            raise RuntimeStoreError('storage_unavailable')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, RuntimeStoreError):
            raise
        raise RuntimeStoreError('storage_unavailable') from exc
    adapter = authority.runner.adapters.get(Platform.LOCAL)
    if adapter is None:
        adapter = LocalSessionAdapter(authority)
        authority.runner.adapters[Platform.LOCAL] = adapter
    if not isinstance(adapter, LocalSessionAdapter) or adapter.authority is not authority:
        raise RuntimeStoreError('runtime_draining')
    live = authority.sessions.get(sid)
    if live is None:
        with store._lock:
            store._ensure_loaded_locked()
            current = store._entries.get(route)
            if current is not None and current.session_id != sid:
                raise RuntimeStoreError('admission_conflict')
            entry.origin = source
            store._entries[route] = entry
        adapter.policies[chat_id] = policy
        authority.sessions[sid] = LiveSession(source, route)
        adapter.register_source(source)
    return SessionRef(authority.profile_id, sid)


def recover_local_sessions(authority, *, schedule=False):
    with authority.db._read_ctx() as conn:
        ids = [row[0][len(POLICY_PREFIX):] for row in conn.execute(
            'SELECT key FROM state_meta WHERE key LIKE ?', (POLICY_PREFIX + '%',))]
    for sid in ids:
        try:
            ref = restore_local_session(authority, sid)
            pending = list_session_admissions(authority.db, session_id=sid)
            if schedule and pending and not any(row['status'] == 'unknown' for row in pending):
                authority._schedule(ref)
        except RuntimeStoreError as exc:
            logging.getLogger(__name__).warning('Local session %s paused: %s', sid, exc.reason)
