"""Exact execution results commit with settlement, before delivery/publication."""
import json
from contextvars import ContextVar

from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, settle_session_input

execution_result: ContextVar[dict | None] = ContextVar("execution_result", default=None)
from hermes_state_terminal import RESULT_PREFIX as _RESULT_PREFIX


def retain_result(db, *, epoch, row, result):
    return settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
                                generation=row['generation'], outcome='completed', result=result)


def finish_result(db, *, epoch, row, response, outcome):
    """Delivery failure cannot rewrite an already committed execution outcome."""
    with db._read_ctx() as conn:
        _epoch(conn, epoch)
        current = _admission(conn, row['admission_id'])
        if current['owner_epoch'] != epoch or current['generation'] != row['generation']:
            raise RuntimeStoreError('stale_generation')
        if current['status'] == 'terminal':
            saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                 (_RESULT_PREFIX + row['admission_id'],)).fetchone()
            if saved is None:
                raise RuntimeStoreError('storage_unavailable')
            result = json.loads(saved[0])
            return dict(current), result['result'].get('final_response') or ''
    settled = settle_session_input(db, epoch=epoch, admission_id=row['admission_id'],
        generation=row['generation'], outcome=outcome,
        result={'result': {'final_response': response or '', 'messages': []}, 'usage': {}})
    return settled, response


def admission_result(db, admission_id):
    with db._read_ctx() as conn:
        row = _admission(conn, admission_id)
        if row['status'] != 'terminal':
            return None
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_RESULT_PREFIX + admission_id,)).fetchone()
        return json.loads(saved[0]) if saved else None
