"""Closed action handlers inside the runtime mutation receipt transaction."""
import time
from hermes_state_runtime import RuntimeStoreError

METADATA_FIELDS = {'title': str, 'archived': bool, 'hidden': bool, 'pinned': bool, 'unread': bool}


def validate_action(operation, payload):
    if not isinstance(payload, dict) or not isinstance(operation, str):
        raise RuntimeStoreError('invalid_params')
    if operation == 'delete' and not payload:
        return
    required = {'rename': {'title'}, 'archive': {'archived'}}
    if operation == 'sidebar':
        valid = bool(payload) and not set(payload) - METADATA_FIELDS.keys()
    else:
        valid = operation in required and set(payload) == required[operation]
    if not valid or any(type(value) is not METADATA_FIELDS[key] for key, value in payload.items()):
        raise RuntimeStoreError('invalid_params')


def apply_action(db, conn, session_id, operation, payload):
    if operation == 'delete':
        return _delete(db, conn, session_id)
    affected = set()
    result = {}
    for key, value in payload.items():
        if key == 'title':
            affected.update(db._set_session_title_in_transaction(
                conn, session_id, value, source=db.TITLE_SOURCE_USER))
            result[key] = conn.execute('SELECT title FROM sessions WHERE id=?', (session_id,)).fetchone()[0]
        else:
            column = 'last_read_at' if key == 'unread' else key
            stored = (0.0 if value else time.time()) if key == 'unread' else int(value)
            affected.update(db._set_lineage_column_in_transaction(conn, column, session_id, stored))
            result[key] = value
    return affected, result


def _delete(db, conn, session_id):
    from hermes_state_mutation_guards import require_idle, delete_targets
    targets = delete_targets(conn, session_id)
    require_idle(db, conn, targets)
    # Completed receipts remain queryable. Until storage has durable tombstones,
    # retaining the row is safer than erasing retry/adoption evidence.
    for sid in targets:
        if (conn.execute('SELECT 1 FROM session_admissions WHERE target_session_id=? LIMIT 1', (sid,)).fetchone()
                or conn.execute('SELECT 1 FROM worker_executions WHERE session_id=? LIMIT 1', (sid,)).fetchone()):
            raise RuntimeStoreError('retained_receipts')
    for sid in targets:
        conn.execute('UPDATE sessions SET parent_session_id=NULL, runtime_revision=runtime_revision+1 WHERE parent_session_id=?', (sid,))
        conn.execute('DELETE FROM messages WHERE session_id=?', (sid,))
    conn.executemany('DELETE FROM sessions WHERE id=?', [(sid,) for sid in targets])
    db._delete_unreferenced_system_prompts(conn)
    return set(), {'deleted_ids': targets}

