"""Transcript mutation guards evaluated on the receipt's writer connection."""
from hermes_state_runtime import RuntimeStoreError


def require_idle(db, conn, session_ids):
    for sid in session_ids:
        admissions = conn.execute("SELECT status FROM session_admissions WHERE target_session_id=? AND status!='terminal'", (sid,)).fetchall()
        workers = conn.execute("SELECT status FROM worker_executions WHERE session_id=? AND status!='terminal'", (sid,)).fetchall()
        states = {row[0] for row in [*admissions, *workers]}
        if 'unknown' in states:
            raise RuntimeStoreError('unknown_execution')
        if states:
            raise RuntimeStoreError('session_busy')
        db._check_transcript_write_guards(conn, sid, None,
            reject_active_turn_lease=True, reject_active_compression_lock=True)


def delete_targets(conn, session_id):
    from hermes_state_sessions import _collect_delegate_child_ids
    return [session_id, *sorted(_collect_delegate_child_ids(conn, [session_id]))]
