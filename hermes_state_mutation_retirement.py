"""Durable deletion fences outside prunable transcript rows."""
import json
from hermes_state_runtime import admission_fingerprint, _text

RETIRED_PREFIX = 'gateway.retired_session.v1.'


def retired_session(db, session_id):
    with db._read_ctx() as conn:
        return conn.execute('SELECT 1 FROM state_meta WHERE key=?',
                            (RETIRED_PREFIX + session_id,)).fetchone() is not None


def has_mutation_receipt(db, principal_id, session_id, request_id):
    for value in (principal_id, session_id, request_id):
        _text(value)
    key = 'gateway.mutation.v1.' + admission_fingerprint(
        canonical_target=session_id, payload={'principal': principal_id, 'request': request_id})
    with db._read_ctx() as conn:
        return conn.execute('SELECT 1 FROM state_meta WHERE key=?', (key,)).fetchone() is not None


def retire_routes(conn, session_ids):
    for sid in session_ids:
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     (RETIRED_PREFIX + sid, '{}'))
    targets = set(session_ids)
    for row in conn.execute('SELECT scope,session_key,entry_json FROM gateway_routing').fetchall():
        if json.loads(row['entry_json']).get('session_id') in targets:
            conn.execute('DELETE FROM gateway_routing WHERE scope=? AND session_key=?',
                         (row['scope'], row['session_key']))
