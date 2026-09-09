"""Private owner-side reservation for a concrete already-started admission.

The producer retains the Popen handle and holds the child's bootstrap pipe until
this returns. Public worker.register remains idle-only for unreserved callers.
"""
import os
import secrets
import subprocess

from gateway.session_admission import admission_fingerprint
from hermes_state_runtime import RuntimeStoreError, _admission, _epoch, _secret_digest, _session


def reserve_admission_worker(authority, *, admission_id, process, principal_id):
    """Return a private bootstrap scope; never accept caller-selected kind/target.

    Only the owner process may reserve its live direct exec child. The admission
    supplies physical session and generation; its durable principal must match
    the producer. Do not publish the returned secret on any client event stream.
    """
    import psutil
    authority._require_admission_open()
    if not isinstance(process, subprocess.Popen) or process.poll() is not None:
        raise RuntimeStoreError('worker_not_live')
    try:
        child = psutil.Process(process.pid)
        if child.ppid() != os.getpid() or child.status() == psutil.STATUS_ZOMBIE:
            raise RuntimeStoreError('permission_denied')
        birth = child.create_time()
    except psutil.Error as exc:
        raise RuntimeStoreError('worker_not_live') from exc
    secret = secrets.token_urlsafe(32)

    def write(conn):
        _epoch(conn, authority.epoch)
        admission = _admission(conn, admission_id)
        if admission['status'] != 'started' or admission['owner_epoch'] != authority.epoch:
            raise RuntimeStoreError('producer_not_started')
        if admission['principal_id'] != principal_id:
            raise RuntimeStoreError('permission_denied')
        sid, generation = admission['target_session_id'], admission['generation']
        if _session(conn, sid)['runtime_generation'] != generation:
            raise RuntimeStoreError('stale_generation')
        execution_id = 'admission-worker:' + admission_id
        if conn.execute('SELECT 1 FROM worker_executions WHERE execution_id=?', (execution_id,)).fetchone():
            raise RuntimeStoreError('admission_conflict')
        if conn.execute("SELECT 1 FROM worker_executions WHERE session_id=? AND status!='terminal'", (sid,)).fetchone():
            raise RuntimeStoreError('stale_generation')
        scope = dict(profile_id=authority.profile_id, session_id=sid, execution_id=execution_id,
                     generation=generation, pid=process.pid, birth=birth, secret=secret)
        claim = admission_fingerprint(canonical_target=sid, payload=scope | {'principal': principal_id})
        conn.execute("INSERT INTO worker_executions(execution_id,session_id,kind,owner_epoch,generation,status,adoption_digest) "
                     "VALUES(?,?,'compute',?,?,'registered',?)",
                     (execution_id, sid, authority.epoch, generation, _secret_digest(claim)))
        return scope | {'epoch': authority.epoch}

    return authority.db._execute_write(write)


def lose_admission_worker(authority, row, scope):
    """Revoke a private assignment and pause its FIFO; no inference replay.

    This also fences a result already in the pipe at loss/shutdown. Retention
    requires a still-started admission even when the generation has not changed.
    """
    def write(conn):
        _epoch(conn, scope['epoch'])
        current = _admission(conn, row['admission_id'])
        if (current['status'] != 'started' or current['owner_epoch'] != scope['epoch']
                or current['generation'] != scope['generation']):
            raise RuntimeStoreError('stale_generation')
        worker = conn.execute('SELECT * FROM worker_executions WHERE execution_id=?',
                              (scope['execution_id'],)).fetchone()
        if worker is None or worker['generation'] != scope['generation'] or worker['owner_epoch'] != scope['epoch']:
            raise RuntimeStoreError('stale_generation')
        conn.execute("UPDATE worker_executions SET status='terminal' WHERE execution_id=?", (scope['execution_id'],))
        conn.execute("UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (row['admission_id'],))
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?', (current['target_session_id'],))
    authority.db._execute_write(write)
