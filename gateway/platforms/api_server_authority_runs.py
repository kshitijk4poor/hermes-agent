"""API run controls resolve durable claims, never adapter agent/task ownership."""
from gateway.session_contract import Principal, SessionRef
from gateway.session_results import admission_result
from hermes_state_runtime import RuntimeStoreError, _row


def run_admission(adapter, run_id):
    authority = getattr(adapter.gateway_runner, 'session_authority', None)
    if authority is None:
        return None
    with authority.db._read_ctx() as conn:
        rows = conn.execute("SELECT * FROM session_admissions WHERE principal_id='api' AND request_id=?", (run_id,)).fetchall()
    if len(rows) > 1:
        raise RuntimeStoreError('admission_conflict')
    return (authority, _row(rows[0])) if rows else None


def run_projection(adapter, run_id):
    owned = run_admission(adapter, run_id)
    if owned is None:
        return None
    authority, row = owned
    status = {'queued': 'queued', 'started': 'running', 'unknown': 'interrupted', 'terminal': row['outcome']}.get(row['status'])
    saved = admission_result(authority.db, row['admission_id'])
    result = saved.get('result', {}) if saved else {}
    if row['status'] == 'terminal' and result.get('failed'):
        status = 'failed'
    return {'run_id': run_id, 'status': status, 'session_id': row['target_session_id'],
            'admission_id': row['admission_id'], 'execution_generation': row['generation'],
            'output': result.get('final_response', ''), 'usage': saved.get('usage', {}) if saved else {}}


async def stop_run(adapter, run_id):
    owned = run_admission(adapter, run_id)
    if owned is None:
        raise RuntimeStoreError('not_found')
    authority, row = owned
    ref = SessionRef(authority.profile_id, row['target_session_id'])
    actor = Principal('api', authority.profile_id,
                      frozenset({'session:submit', 'session:control'}), 'api-run:' + run_id)
    if row['status'] == 'queued':
        await authority.cancel_queued(actor, ref, row['admission_id'])
        adapter._stopping_run_ids.add(run_id)
        waiter = authority.waiters.pop(row['admission_id'], None)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
    elif row['status'] == 'started':
        await authority.interrupt(actor, ref, row['generation'])
        adapter._stopping_run_ids.add(run_id)
        return {'run_id': run_id, 'status': 'stopping', 'admission_id': row['admission_id']}
    elif row['status'] == 'unknown':
        raise RuntimeStoreError('unknown_execution')
    return run_projection(adapter, run_id)
