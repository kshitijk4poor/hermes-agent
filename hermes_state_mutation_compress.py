"""Commit a prepared summary, physical successor, generation and receipt atomically."""
import uuid
from hermes_state_mutation_prepared import validate_prepared


def compress_in_transaction(db, conn, session_id, payload, prepared):
    snapshot = validate_prepared(db, conn, session_id, prepared)
    policy = snapshot['receipt']['policy']
    target = snapshot['target']
    child = uuid.uuid4().hex
    from hermes_state_worker_compression import publish_on_connection
    # No worker/turn/compactor can be active: validate_prepared checks those
    # obligations on this exact connection. No separate legacy transaction runs.
    publish_on_connection(db, conn, parent_session_id=target, child_session_id=child,
        source=policy['source'], model=policy['model'], messages=prepared['messages'],
        model_config={'_compressed_from': target}, cwd=policy['cwd'], require_compression_lease=False)
    conn.execute('UPDATE sessions SET runtime_generation=runtime_generation+1 WHERE id=?', (session_id,))
    generation = conn.execute('SELECT runtime_generation FROM sessions WHERE id=?', (session_id,)).fetchone()[0]
    return set(), {'target_session_id': child, 'previous_target_session_id': target,
                   'execution_generation': generation, 'message_count': len(prepared['messages'])}
