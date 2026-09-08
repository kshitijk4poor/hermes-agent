"""Canonical runtime transactions; callers authorize and resolve session identity.

No execution, transport, or authority objects live here. Every mutation uses the
SessionDB transaction owner, including its inode guard and SQLite retry policy.
"""
import json
import uuid

from gateway.session_admission import admission_fingerprint


class RuntimeStoreError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise RuntimeStoreError('invalid_params')
    return value


def _json(value):
    if not isinstance(value, dict):
        raise RuntimeStoreError('invalid_params')
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeStoreError('invalid_params') from exc


def _epoch(conn, epoch):
    row = conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()
    if type(epoch) is not int or row is None or row[0] != epoch:
        raise RuntimeStoreError('stale_epoch')


def _session(conn, session_id):
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row is None:
        raise RuntimeStoreError('not_found')
    return row


def _admission(conn, admission_id):
    row = conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission_id,)).fetchone()
    if row is None:
        raise RuntimeStoreError('not_found')
    return row


def _row(row):
    if row is None:
        return None
    result = dict(row)
    result['payload'] = json.loads(result.pop('payload_json'))
    result.pop('payload_digest')
    result.pop('lineage_json')
    return result


def begin_runtime_epoch(db, *, instance_id: str) -> int:
    """Caller MUST hold the profile ownership lock, not merely an endpoint ticket."""
    _text(instance_id)
    def write(conn):
        conn.execute('''INSERT INTO runtime_epoch(singleton,epoch,instance_id) VALUES(1,1,?)
            ON CONFLICT(singleton) DO UPDATE SET epoch=runtime_epoch.epoch+1, instance_id=excluded.instance_id''', (instance_id,))
        return conn.execute('SELECT epoch FROM runtime_epoch WHERE singleton=1').fetchone()[0]
    return db._execute_write(write)


def admit_session_input(db, *, epoch: int, principal_id: str, session_id: str,
                        request_id: str, payload: dict, intent: str = 'queue') -> dict:
    for value in (principal_id, session_id, request_id):
        _text(value)
    if intent not in ('queue', 'steer', 'redirect'):
        raise RuntimeStoreError('invalid_params')
    encoded = _json(payload)
    digest = admission_fingerprint(canonical_target=session_id, payload={'input': json.loads(encoded), 'intent': intent})
    def write(conn):
        _epoch(conn, epoch)
        _session(conn, session_id)
        old = conn.execute('''SELECT * FROM session_admissions
            WHERE principal_id=? AND target_session_id=? AND request_id=?''', (principal_id, session_id, request_id)).fetchone()
        if old is not None:
            if old['payload_digest'] != digest:
                raise RuntimeStoreError('admission_conflict')
            return _row(old)
        admission_id = uuid.uuid4().hex
        conn.execute('''INSERT INTO session_admissions(admission_id,request_id,principal_id,
            target_session_id,lineage_json,payload_json,payload_digest,intent,status,owner_epoch)
            VALUES(?,?,?,?,?,?,?,?,'queued',?)''',
            (admission_id, request_id, principal_id, session_id, json.dumps([session_id]), encoded, digest, intent, epoch))
        return _row(_admission(conn, admission_id))
    return db._execute_write(write)


def get_session_admission(db, *, admission_id: str) -> dict | None:
    with db._read_ctx() as conn:
        return _row(conn.execute('SELECT * FROM session_admissions WHERE admission_id=?', (admission_id,)).fetchone())


def list_session_admissions(db, *, session_id: str, pending_only: bool = True) -> list[dict]:
    with db._read_ctx() as conn:
        return [_row(row) for row in conn.execute('''SELECT * FROM session_admissions
            WHERE target_session_id=? AND (?=0 OR status!='terminal') ORDER BY seq''', (session_id, int(pending_only)))]


def claim_session_input(db, *, epoch: int, session_id: str) -> dict | None:
    def write(conn):
        _epoch(conn, epoch)
        session = _session(conn, session_id)
        blocked = conn.execute("SELECT status FROM session_admissions WHERE target_session_id=? AND status IN ('started','unknown')", (session_id,)).fetchall()
        if any(row[0] == 'unknown' for row in blocked):
            raise RuntimeStoreError('unknown_execution')
        if conn.execute("SELECT 1 FROM worker_executions WHERE session_id=? AND status='unknown'", (session_id,)).fetchone():
            raise RuntimeStoreError('unknown_execution')
        if blocked or conn.execute("SELECT 1 FROM worker_executions WHERE session_id=? AND status IN ('registered','running')", (session_id,)).fetchone():
            return None
        row = conn.execute("SELECT * FROM session_admissions WHERE target_session_id=? AND status='queued' ORDER BY seq LIMIT 1", (session_id,)).fetchone()
        if row is None:
            return None
        generation = session['runtime_generation'] + 1
        conn.execute('UPDATE sessions SET runtime_generation=? WHERE id=?', (generation, session_id))
        changed = conn.execute("UPDATE session_admissions SET status='started',owner_epoch=?,generation=? WHERE admission_id=? AND status='queued'", (epoch, generation, row['admission_id']))
        if changed.rowcount != 1:
            raise RuntimeStoreError('stale_generation')
        return _row(_admission(conn, row['admission_id']))
    return db._execute_write(write)


def settle_session_input(db, *, epoch: int, admission_id: str, generation: int, outcome: str) -> dict:
    if outcome not in ('completed', 'interrupted', 'rejected', 'failed'):
        raise RuntimeStoreError('invalid_params')
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        session = _session(conn, row['target_session_id'])
        if (row['status'] != 'started' or row['owner_epoch'] != epoch
                or type(generation) is not int or row['generation'] != generation
                or session['runtime_generation'] != generation):
            raise RuntimeStoreError('stale_generation')
        conn.execute("UPDATE worker_executions SET status='terminal' WHERE session_id=? AND generation=? AND owner_epoch=?", (row['target_session_id'], generation, epoch))
        conn.execute("UPDATE session_admissions SET status='terminal',outcome=? WHERE admission_id=?", (outcome, admission_id))
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?', (row['target_session_id'],))
        return _row(_admission(conn, admission_id))
    return db._execute_write(write)


def cancel_session_input(db, *, epoch: int, admission_id: str) -> dict:
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        if row['status'] == 'unknown':
            raise RuntimeStoreError('unknown_execution')
        if row['status'] == 'started':
            raise RuntimeStoreError('stale_generation')
        if row['status'] == 'queued':
            conn.execute("UPDATE session_admissions SET status='terminal',outcome='cancelled' WHERE admission_id=?", (admission_id,))
        return _row(_admission(conn, admission_id))
    return db._execute_write(write)


def recover_session_inputs(db, *, epoch: int) -> int:
    """Never replay started work. Live worker adoption is a separate explicit operation."""
    def write(conn):
        _epoch(conn, epoch)
        conn.execute("UPDATE worker_executions SET status='unknown' WHERE status IN ('registered','running') AND owner_epoch!=?", (epoch,))
        return conn.execute("UPDATE session_admissions SET status='unknown' WHERE status='started' AND owner_epoch!=?", (epoch,)).rowcount
    return db._execute_write(write)


def mutate_runtime_session(db, *, epoch: int, principal_id: str, session_id: str,
                           request_id: str, expected_revision: int,
                           operation: str, payload: dict) -> dict:
    """Commit a closed metadata edit and its retry receipt in the same transaction.

    Caller authorizes the principal and resolves the canonical session. These
    metadata edits do not stop execution. Existing direct writers must migrate
    before this seam can provide universal revision fencing.
    """
    for value in (principal_id, session_id, request_id):
        _text(value)
    if type(expected_revision) is not int or expected_revision < 0:
        raise RuntimeStoreError('invalid_params')
    fields = {'rename': ('title', str), 'archive': ('archived', bool)}
    if not isinstance(operation, str) or operation not in fields or not isinstance(payload, dict):
        raise RuntimeStoreError('invalid_params')
    field, value_type = fields[operation]
    if set(payload) != {field} or type(payload[field]) is not value_type:
        raise RuntimeStoreError('invalid_params')
    # Snapshot caller data before waiting for the writer lock.
    payload = json.loads(_json(payload))
    key = 'gateway.mutation.v1.' + admission_fingerprint(
        canonical_target=session_id, payload={'principal': principal_id, 'request': request_id})
    digest = admission_fingerprint(canonical_target=session_id, payload={
        'operation': operation, 'payload': payload, 'expected_revision': expected_revision})

    def write(conn):
        _epoch(conn, epoch)
        old = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if old is not None:
            receipt = json.loads(old[0])
            if receipt['digest'] != digest:
                raise RuntimeStoreError('admission_conflict')
            return receipt['result']
        session = _session(conn, session_id)
        if session['runtime_revision'] != expected_revision:
            raise RuntimeStoreError('revision_conflict')
        if operation == 'rename':
            affected = db._set_session_title_in_transaction(
                conn, session_id, payload['title'], source=db.TITLE_SOURCE_USER)
        else:
            affected = db._set_lineage_column_in_transaction(
                conn, 'archived', session_id, int(payload['archived']))
        conn.executemany('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?',
                         [(target,) for target in affected])
        updated = _session(conn, session_id)
        result = {'session_id': session_id, 'revision': updated['runtime_revision'],
                  'operation': operation, field: updated[field]}
        if operation == 'archive':
            result[field] = bool(result[field])
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     (key, _json({'digest': digest, 'result': result})))
        return result
    return db._execute_write(write)


_IMPORT_KEY = 'gateway.prompt_admissions_import.v1'


def _canonical_chain(conn, session_id):
    # Same selector as SessionDB.get_compression_chain, on OUR transaction connection.
    from hermes_state_compression import _CHAIN_STEP_SQL
    _session(conn, session_id)
    chain = [session_id]
    for _ in range(100):
        child = conn.execute(_CHAIN_STEP_SQL, (chain[-1],)).fetchone()
        if child is None:
            return chain
        if child['id'] in chain:
            raise RuntimeStoreError('admission_conflict')
        chain.append(child['id'])
    raise RuntimeStoreError('admission_conflict')


def _import_legacy_row(conn, row, epoch, principal_id):
    for field in ('admission_id', 'target_session_id', 'root'):
        _text(row[field])
    lineage = json.loads(row['lineage'])
    if not isinstance(lineage, list) or row['target_session_id'] not in lineage:
        raise RuntimeStoreError('invalid_params')
    payload = json.loads(row['payload'])
    encoded = _json(payload)
    intent = payload.get('intent', 'queue')
    intent = 'redirect' if intent == 'interrupt' else intent
    if intent not in ('queue', 'steer', 'redirect') or row['status'] not in ('queued', 'started', 'unknown', 'terminal'):
        raise RuntimeStoreError('invalid_params')
    chain = _canonical_chain(conn, row['target_session_id'])
    target = chain[-1]
    status = 'unknown' if row['status'] == 'started' else row['status']
    generation = row['generation']
    if generation is None and status == 'unknown':
        generation = _session(conn, target)['runtime_generation'] + 1
    if generation is not None and (type(generation) is not int or generation < 0):
        raise RuntimeStoreError('invalid_params')
    digest = admission_fingerprint(canonical_target=target, payload={'input': payload, 'intent': intent})
    conn.execute("""INSERT INTO session_admissions(admission_id,request_id,principal_id,
        target_session_id,lineage_json,payload_json,payload_digest,intent,status,outcome,owner_epoch,generation)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (row['admission_id'], row['admission_id'], principal_id,
        target, json.dumps(chain), encoded, digest, intent, status, row['outcome'], epoch, generation))
    if generation is not None:
        conn.execute('UPDATE sessions SET runtime_generation=MAX(runtime_generation,?) WHERE id=?', (generation, target))


def import_legacy_session_admissions(db, *, epoch: int, source_path, principal_id: str,
                                     writers_drained: bool) -> int:
    """Import a frozen legacy snapshot; original file remains rollback evidence.

    The caller proves the old writers are drained before invoking this function.
    Fingerprinting uses SQL snapshot data, never a raw open/close on a live inode.
    """
    from contextlib import closing
    from pathlib import Path
    import hashlib
    import sqlite3
    from hermes_cli.sqlite_safe_read import connect_tracked
    if writers_drained is not True:
        raise RuntimeStoreError('invalid_params')
    _text(principal_id)
    source_path = Path(source_path).resolve(strict=True)
    with closing(connect_tracked(source_path.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        rows = [dict(r) for r in source.execute('SELECT * FROM admissions ORDER BY seq')]
        executions = []
        if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'").fetchone():
            executions = [dict(r) for r in source.execute('SELECT * FROM executions ORDER BY root')]
        fingerprint = hashlib.sha256(_json({'rows': rows, 'executions': executions}).encode('utf-8')).hexdigest()
        marker = _json({'source': str(source_path), 'fingerprint': fingerprint, 'imported_id_count': len(rows), 'principal_id': principal_id})
        def write(conn):
            _epoch(conn, epoch)
            old = conn.execute('SELECT value FROM state_meta WHERE key=?', (_IMPORT_KEY,)).fetchone()
            if old is not None:
                if old[0] != marker:
                    raise RuntimeStoreError('admission_conflict')
                return 0
            for execution in executions:
                generation = execution['generation']
                if type(generation) is not int or generation < 0:
                    raise RuntimeStoreError('invalid_params')
                target = _canonical_chain(conn, _text(execution['root']))[-1]
                conn.execute('UPDATE sessions SET runtime_generation=MAX(runtime_generation,?) WHERE id=?', (generation, target))
            for row in rows:
                _import_legacy_row(conn, row, epoch, principal_id)
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (_IMPORT_KEY, marker))
            return len(rows)
        return db._execute_write(write)


def resolve_unknown_session_input(db, *, epoch: int, admission_id: str, generation: int) -> dict:
    """Explicit operator acknowledgement; resolves uncertainty, never requeues it."""
    def write(conn):
        _epoch(conn, epoch)
        row = _admission(conn, admission_id)
        if type(generation) is not int or row['status'] != 'unknown' or row['generation'] != generation:
            raise RuntimeStoreError('stale_generation')
        conn.execute("UPDATE worker_executions SET status='terminal' WHERE session_id=? AND generation=? AND owner_epoch=?", (row['target_session_id'], generation, row['owner_epoch']))
        conn.execute("UPDATE session_admissions SET status='terminal',outcome='interrupted' WHERE admission_id=?", (admission_id,))
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?', (row['target_session_id'],))
        return _row(_admission(conn, admission_id))
    return db._execute_write(write)


def _worker_public(row):
    result = dict(row)
    result.pop('adoption_digest')
    return result


def _worker_assignment(conn, execution_id, session_id, generation):
    row = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (execution_id,)).fetchone()
    if row is None:
        raise RuntimeStoreError('not_found')
    if row['session_id'] != session_id:
        raise RuntimeStoreError('permission_denied')
    if (type(generation) is not int or row['generation'] != generation
            or _session(conn, session_id)['runtime_generation'] != generation):
        raise RuntimeStoreError('stale_generation')
    return row


def _secret_digest(secret):
    import hashlib
    _text(secret)
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def register_worker_execution(db, *, epoch: int, execution_id: str, session_id: str,
                              generation: int, kind: str, adoption_secret: str) -> dict:
    for value in (execution_id, session_id):
        _text(value)
    if kind not in ('cron', 'child', 'compute', 'kanban'):
        raise RuntimeStoreError('invalid_params')
    digest = _secret_digest(adoption_secret)
    def write(conn):
        _epoch(conn, epoch)
        session = _session(conn, session_id)
        if type(generation) is not int or session['runtime_generation'] != generation:
            raise RuntimeStoreError('stale_generation')
        old = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?', (execution_id,)).fetchone()
        if old is not None:
            if (old['session_id'], old['generation'], old['kind'], old['owner_epoch'], old['adoption_digest']) != (session_id, generation, kind, epoch, digest):
                raise RuntimeStoreError('admission_conflict')
            return _worker_public(old)
        if conn.execute("SELECT 1 FROM worker_executions WHERE session_id=? AND status!='terminal'", (session_id,)).fetchone():
            raise RuntimeStoreError('stale_generation')
        if conn.execute("SELECT 1 FROM session_admissions WHERE target_session_id=? AND status='unknown'", (session_id,)).fetchone():
            raise RuntimeStoreError('unknown_execution')
        conn.execute("""INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest)
            VALUES(?,?,?,?,?,'registered',?)""", (execution_id, session_id, kind, epoch, generation, digest))
        return _worker_public(_worker_assignment(conn, execution_id, session_id, generation))
    return db._execute_write(write)


def adopt_worker_execution(db, *, epoch: int, execution_id: str, session_id: str,
                           generation: int, adoption_secret: str) -> dict:
    """Authority first verifies the original producer claim and live worker proof."""
    import hmac
    digest = _secret_digest(adoption_secret)
    def write(conn):
        _epoch(conn, epoch)
        row = _worker_assignment(conn, execution_id, session_id, generation)
        if row['status'] == 'terminal':
            raise RuntimeStoreError('stale_generation')
        if not hmac.compare_digest(row['adoption_digest'], digest):
            raise RuntimeStoreError('permission_denied')
        conn.execute("""UPDATE session_admissions SET owner_epoch=?,status='started'
            WHERE target_session_id=? AND generation=? AND owner_epoch=?
            AND status IN ('started','unknown')""", (epoch, session_id, generation, row['owner_epoch']))
        conn.execute("UPDATE worker_executions SET owner_epoch=?,status='running' WHERE execution_id=?", (epoch, execution_id))
        return _worker_public(_worker_assignment(conn, execution_id, session_id, generation))
    return db._execute_write(write)


def persist_worker_message(db, *, epoch: int, execution_id: str, session_id: str,
                           generation: int, sequence: int, role: str, content: str) -> dict:
    """Typed text append primitive, NOT a general remote SessionDB implementation.

    Structured tools/reasoning/usage/compression require their own typed operations.
    No caller-supplied callable can commit inside this transaction.
    """
    import time
    if type(sequence) is not int or sequence < 1 or role not in ('user', 'assistant', 'system') or not isinstance(content, str):
        raise RuntimeStoreError('invalid_params')
    digest = admission_fingerprint(canonical_target=session_id, payload={'operation': 'append_text', 'role': role, 'content': content})
    def write(conn):
        _epoch(conn, epoch)
        row = _worker_assignment(conn, execution_id, session_id, generation)
        if row['owner_epoch'] != epoch:
            raise RuntimeStoreError('stale_epoch')
        old = conn.execute('SELECT * FROM worker_receipts WHERE execution_id=? AND sequence=?', (execution_id, sequence)).fetchone()
        if old is not None:
            if old['payload_digest'] != digest:
                raise RuntimeStoreError('admission_conflict')
            return json.loads(old['result_json'])
        if row['status'] == 'terminal':
            raise RuntimeStoreError('stale_generation')
        if sequence != row['last_sequence'] + 1:
            raise RuntimeStoreError('invalid_params')
        db._check_transcript_write_guards(conn, session_id, None)
        now = time.time()
        message = conn.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)', (session_id, role, content, now))
        conn.execute('UPDATE sessions SET message_count=message_count+1,last_activity_at=?,runtime_revision=runtime_revision+1 WHERE id=?', (now, session_id))
        result = {'message_id': message.lastrowid}
        conn.execute('INSERT INTO worker_receipts(execution_id,sequence,payload_digest,result_json) VALUES(?,?,?,?)', (execution_id, sequence, digest, _json(result)))
        conn.execute("UPDATE worker_executions SET last_sequence=?,status='running' WHERE execution_id=?", (sequence, execution_id))
        return result
    return db._execute_write(write)


def finish_worker_execution(db, *, epoch: int, execution_id: str, session_id: str,
                            generation: int) -> dict:
    def write(conn):
        _epoch(conn, epoch)
        row = _worker_assignment(conn, execution_id, session_id, generation)
        if row['owner_epoch'] != epoch:
            raise RuntimeStoreError('stale_epoch')
        if row['status'] == 'terminal':
            return _worker_public(row)
        conn.execute("UPDATE worker_executions SET status='terminal' WHERE execution_id=?", (execution_id,))
        result = _worker_public(row)
        result['status'] = 'terminal'
        return result
    return db._execute_write(write)
