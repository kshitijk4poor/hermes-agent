"""Closed action handlers inside the runtime mutation receipt transaction."""
import time
from hermes_state_runtime import RuntimeStoreError

METADATA_FIELDS = {'title': str, 'archived': bool, 'hidden': bool, 'pinned': bool, 'unread': bool}


def validate_action(operation, payload):
    if not isinstance(payload, dict) or not isinstance(operation, str):
        raise RuntimeStoreError('invalid_params')
    if operation == 'branch' and (not payload or (set(payload) == {'title'} and isinstance(payload['title'], str))):
        return
    if operation in {'delete', 'reset'} and not payload:
        return
    if operation == 'import' and set(payload) == {'sessions'} and isinstance(payload['sessions'], list):
        return
    if (operation == 'rewind' and 'target_message_id' in payload
            and not set(payload) - {'target_message_id', 'preserve_compaction_handoff'}
            and type(payload['target_message_id']) is int and payload['target_message_id'] > 0
            and type(payload.get('preserve_compaction_handoff', False)) is bool):
        return
    required = {'rename': {'title'}, 'archive': {'archived'}}
    if operation == 'sidebar':
        valid = bool(payload) and not set(payload) - METADATA_FIELDS.keys()
    else:
        valid = operation in required and set(payload) == required[operation]
    if not valid or any(type(value) is not METADATA_FIELDS[key] for key, value in payload.items()):
        raise RuntimeStoreError('invalid_params')


def apply_action(db, conn, session_id, operation, payload):
    from hermes_state_mutation_reset import reset_in_transaction
    from hermes_state_mutation_branch import branch_in_transaction
    handlers = {'delete': _delete, 'import': _import, 'rewind': _rewind, 'reset': reset_in_transaction,
                'branch': branch_in_transaction}
    if operation in handlers:
        return handlers[operation](db, conn, session_id, payload)
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


def _delete(db, conn, session_id, payload):
    from hermes_state_mutation_guards import require_idle, delete_targets
    targets = delete_targets(conn, session_id)
    require_idle(db, conn, targets)
    # Completed receipts remain queryable. Until storage has durable tombstones,
    # retaining the row is safer than erasing retry/adoption evidence.
    for sid in targets:
        if (conn.execute('SELECT 1 FROM session_admissions WHERE target_session_id=? LIMIT 1', (sid,)).fetchone()
                or conn.execute('SELECT 1 FROM worker_executions WHERE session_id=? LIMIT 1', (sid,)).fetchone()):
            raise RuntimeStoreError('retained_receipts')
    from hermes_state_mutation_retirement import retire_routes
    retire_routes(conn, targets)
    for sid in targets:
        db._bump_conversation_generation(conn, sid, 'session_reset')
        conn.execute('UPDATE sessions SET parent_session_id=NULL, runtime_revision=runtime_revision+1 WHERE parent_session_id=?', (sid,))
        conn.execute('DELETE FROM messages WHERE session_id=?', (sid,))
    conn.executemany('DELETE FROM sessions WHERE id=?', [(sid,) for sid in targets])
    db._delete_unreferenced_system_prompts(conn)
    return set(), {'deleted_ids': targets}


def _rewind(db, conn, session_id, payload):
    from hermes_state_mutation_guards import require_idle
    from hermes_state_mutation_transcript import rewind_in_transaction
    require_idle(db, conn, [session_id])
    target, ids, head, replacement = rewind_in_transaction(db, conn, session_id,
        payload['target_message_id'], preserve_compaction_handoff=payload.get('preserve_compaction_handoff', False))
    target['content'] = db._decode_content(target.get('content'))
    conn.execute('UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id=?', (session_id,))
    return {session_id}, {'rewound_count': len(ids), 'target_message': target,
        'new_head_id': head, 'replacement_message_id': replacement}


def _import(db, conn, session_id, payload):
    rows = payload['sessions']
    if len(rows) > db._IMPORT_MAX_SESSIONS:
        raise RuntimeStoreError('invalid_params')
    normalized, errors = db._validate_import_payload(rows)
    if errors or not normalized or session_id != normalized[0]['session']['id']:
        raise RuntimeStoreError('invalid_params')
    imported, skipped, parents = [], [], []
    for item in normalized:
        raw = item['session']
        sid = raw['id']
        from hermes_state_mutation_retirement import RETIRED_PREFIX
        if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (RETIRED_PREFIX + sid,)).fetchone():
            raise RuntimeStoreError('admission_conflict')
        if conn.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone():
            skipped.append(sid)
            continue
        db._import_session_row(conn, raw, item['messages'], sid)
        if raw.get('parent_session_id'):
            parents.append((sid, raw['parent_session_id']))
        imported.append(sid)
    detached = db._attach_import_parents(conn, parents)
    return set(imported), {'ok': True, 'imported': len(imported), 'skipped': len(skipped),
        'imported_ids': imported, 'skipped_ids': skipped, 'detached': detached, 'errors': []}
