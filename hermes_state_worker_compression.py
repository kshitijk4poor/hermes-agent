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


WORKER_COMPRESSION_HANDLERS = {
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
