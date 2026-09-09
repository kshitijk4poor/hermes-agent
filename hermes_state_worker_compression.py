"""Closed compression operations on the worker receipt's connection.

No handler opens a connection or invokes a self-committing SessionDB method.
"""
from functools import partial
import math
import time

from hermes_state_runtime import RuntimeStoreError, _text
from hermes_state_compression import _claim_lease_row


def _fields(payload, required, optional=()):
    if not isinstance(payload, dict) or set(payload) - set(required) - set(optional) or set(required) - set(payload):
        raise RuntimeStoreError('invalid_params')


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise RuntimeStoreError('invalid_params')
    return value


def worker_compression_lock(db, conn, sid, payload, *, action):
    if action == 'holder':
        _fields(payload, ())
        row = conn.execute('SELECT holder FROM compression_locks WHERE session_id=? AND expires_at>?',
                           (sid, time.time())).fetchone()
        return {'value': row[0] if row else None}
    _fields(payload, ('holder',), () if action == 'release' else ('ttl_seconds',))
    holder = _text(payload['holder'])
    now = time.time()
    ttl = _number(payload.get('ttl_seconds', 300.0))
    if not 0.1 <= ttl <= 3600:
        raise RuntimeStoreError('invalid_params')
    if action == 'acquire':
        from hermes_state import _compression_lock_holder_process_is_dead
        value = _claim_lease_row(conn, 'compression_locks', 'session_id', sid, holder, now, now + ttl,
                                lambda h, e: e < now or _compression_lock_holder_process_is_dead(h))[0]
    elif action == 'renew':
        value = conn.execute('UPDATE compression_locks SET expires_at=? WHERE session_id=? AND holder=?',
                             (now + ttl, sid, holder)).rowcount > 0
    else:
        conn.execute('DELETE FROM compression_locks WHERE session_id=? AND holder=?', (sid, holder))
        value = None
    return {'value': value}


def worker_cooldown_record(db, conn, sid, payload):
    _fields(payload, ('cooldown_until', 'error'))
    deadline = _number(payload['cooldown_until'])
    error = payload['error']
    if error is not None and not isinstance(error, str):
        raise RuntimeStoreError('invalid_params')
    conn.execute('UPDATE sessions SET compression_failure_cooldown_until = CASE '
                 'WHEN compression_failure_cooldown_until IS NOT NULL AND compression_failure_cooldown_until > ? '
                 'THEN compression_failure_cooldown_until ELSE ? END, compression_failure_error=? WHERE id=?',
                 (deadline, deadline, error, sid))
    return {'value': None}


def worker_cooldown_restore(db, conn, sid, payload):
    _fields(payload, ('snapshot',))
    snapshot = payload['snapshot']
    _fields(snapshot, ('session_exists', 'cooldown_until', 'error'))
    if type(snapshot['session_exists']) is not bool:
        raise RuntimeStoreError('invalid_params')
    if not snapshot['session_exists']:
        raise RuntimeError('cannot restore absent compression cooldown row: session now exists')
    deadline, error = snapshot['cooldown_until'], snapshot['error']
    if deadline is not None:
        _number(deadline)
    if error is not None and not isinstance(error, str):
        raise RuntimeStoreError('invalid_params')
    conn.execute('UPDATE sessions SET compression_failure_cooldown_until=?, compression_failure_error=? WHERE id=?',
                 (deadline, error, sid))
    actual = conn.execute('SELECT compression_failure_cooldown_until,compression_failure_error FROM sessions WHERE id=?',
                          (sid,)).fetchone()
    if actual is None or tuple(actual) != (deadline, error):
        raise RuntimeError('compression cooldown rollback verification failed')
    return {'value': None}


def worker_cooldown_clear(db, conn, sid, payload):
    _fields(payload, ())
    return worker_cooldown_restore(db, conn, sid, {'snapshot': {
        'session_exists': True, 'cooldown_until': None, 'error': None}})


def worker_compression_counter(db, conn, sid, payload, *, column):
    _fields(payload, ('value',))
    value = payload['value']
    if value is not None:
        _number(value)
    if column != 'compression_recovery_deadline' and (type(value) is not int or value < 0):
        raise RuntimeStoreError('invalid_params')
    conn.execute(f'UPDATE sessions SET {column}=? WHERE id=?', (value, sid))
    return {'value': None}


class CompressionSnapshot:
    """Reuse production read algorithms on the receipt connection, never a second handle.

    Only read primitives are provided; this object cannot start/commit a write.
    Wire handlers below select concrete projections, not caller-named methods.
    """
    from hermes_state_compression import SessionCompressionMixin as _C
    from hermes_state_messages import SessionMessagesMixin as _M
    from hermes_state_sessions import SessionSessionsMixin as _S

    get_compression_chain = _C.get_compression_chain
    get_compression_tip = _C.get_compression_tip
    get_compression_lineage = _C.get_compression_lineage
    _is_compression_child_row = _C._is_compression_child_row
    _session_lineage_root_to_tip = _S._session_lineage_root_to_tip
    _is_explicit_branch_session = _S._is_explicit_branch_session
    declared_scope_identity = _S.declared_scope_identity
    _resume_lineage_ids = _M._resume_lineage_ids
    get_conversation_root = _M.get_conversation_root
    resolve_resume_session_id = _M.resolve_resume_session_id
    latest_conversation_boundary = _M.latest_conversation_boundary
    _is_explicit_fork_child_row = _M._is_explicit_fork_child_row

    def __init__(self, db, conn):
        self.db, self.conn = db, conn

    def _read_ctx(self):
        from contextlib import nullcontext
        return nullcontext(self.conn)

    def _read_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def _read_all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def get_session(self, sid):
        from hermes_state_worker_context import worker_context
        return worker_context(self.db, self.conn, sid, {})['session']

    def authorize(self, assigned, target):
        _text(target)
        # Attribution ancestry is readable; unrelated siblings and foreign trees are not.
        allowed = self._session_lineage_root_to_tip(assigned)
        allowed += self.get_compression_chain(assigned)
        if target not in allowed:
            raise RuntimeStoreError('permission_denied')


def worker_lineage_context(db, conn, sid, payload):
    _fields(payload, ('target',))
    view = CompressionSnapshot(db, conn)
    target = payload['target']
    view.authorize(sid, target)
    return {'session': view.get_session(target)}


def worker_lineage(db, conn, sid, payload):
    _fields(payload, ('target',))
    view = CompressionSnapshot(db, conn)
    target = payload['target']
    view.authorize(sid, target)
    return {'readable_ids': view._session_lineage_root_to_tip(sid) + view.get_compression_chain(sid),
            'lineage': view.get_compression_lineage(target),
            'root': view.get_conversation_root(target),
            'tip': view.get_compression_tip(target),
            'resume': view.resolve_resume_session_id(target),
            'identity': view.declared_scope_identity(target)}


def worker_boundary(db, conn, sid, payload):
    _fields(payload, ('session_key', 'source'))
    view = CompressionSnapshot(db, conn)
    row = view.get_session(sid)
    if (row['session_key'], row['source']) != (payload['session_key'], payload['source']):
        raise RuntimeStoreError('permission_denied')
    return {'value': view.latest_conversation_boundary(payload['session_key'], payload['source'])}


def worker_history(db, conn, sid, payload):
    _fields(payload, ('target', 'include_ancestors', 'include_inactive', 'repair_alternation',
                      'include_row_ids', 'include_compacted'))
    if any(type(v) is not bool for k, v in payload.items() if k != 'target'):
        raise RuntimeStoreError('invalid_params')
    view = CompressionSnapshot(db, conn)
    target = payload['target']
    view.authorize(sid, target)
    ids = view._resume_lineage_ids(target) if payload['include_ancestors'] else [target]
    active = db._active_clause(payload['include_inactive'], payload['include_compacted'])
    rows = conn.execute(f'SELECT {db._CONVERSATION_ROW_COLUMNS} FROM messages '
                        f'WHERE session_id IN ({",".join("?" for _ in ids)}){active} ORDER BY id', ids).fetchall()
    if payload['include_compacted']:
        rows = db._dedupe_display_generations(rows)
    return {'messages': db._rows_to_conversation(rows, session_id=target,
        include_ancestors=payload['include_ancestors'], repair_alternation=payload['repair_alternation'],
        include_row_ids=payload['include_row_ids'])}


WORKER_COMPRESSION_HANDLERS = {
    'compression.context': worker_lineage_context,
    'compression.lineage': worker_lineage,
    'compression.boundary': worker_boundary,
    'compression.history': worker_history,
    **{'compression.lock.' + action: partial(worker_compression_lock, action=action)
       for action in ('acquire', 'renew', 'release', 'holder')},
    'compression.cooldown.record': worker_cooldown_record,
    'compression.cooldown.restore': worker_cooldown_restore,
    'compression.cooldown.clear': worker_cooldown_clear,
    **{'compression.' + name: partial(worker_compression_counter, column=column)
       for name, column in (('fallback', 'compression_fallback_streak'),
                            ('ineffective', 'compression_ineffective_count'),
                            ('recovery', 'compression_recovery_deadline'))},
}
