"""Private structured turn results; terminal receipts never precede retention.

A crash between retention and settlement remains unknown, not a retryable result.
The public stream keeps its text projection, not tool arguments or raw history.
"""
import json
from contextvars import ContextVar

execution_result: ContextVar[dict | None] = ContextVar("execution_result", default=None)

from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, _json, _session

_RESULT_PREFIX = 'gateway.admission.result.v1.'


def retain_result(db, *, epoch, row, result):
    encoded = _json(result)

    def write(conn):
        _epoch(conn, epoch)
        current = _admission(conn, row['admission_id'])
        session = _session(conn, current['target_session_id'])
        if (current['status'] != 'started' or current['owner_epoch'] != epoch
                or current['generation'] != row['generation']
                or session['runtime_generation'] != row['generation']):
            raise RuntimeStoreError('stale_generation')
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (_RESULT_PREFIX + row['admission_id'], encoded))
    db._execute_write(write)


def admission_result(db, admission_id):
    with db._read_ctx() as conn:
        row = _admission(conn, admission_id)
        if row['status'] != 'terminal':
            return None
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_RESULT_PREFIX + admission_id,)).fetchone()
        return json.loads(saved[0]) if saved else None
