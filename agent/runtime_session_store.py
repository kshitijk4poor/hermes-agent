"""Explicit worker persistence facade; never opens the canonical SQLite store.

This first operation family is deliberately NOT a complete SessionDB substitute.
No existing cron/child/compute consumer is switched until its inventory is covered.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading


class WorkerPersistenceError(RuntimeError):
    pass


class WorkerRPC:
    """Bounded authenticated calls to an existing owner; never starts a daemon.

    websockets.sync owns a dedicated frame receiver, independent of the caller.
    No owner-loop synchronous self-RPC or SQLite fallback is permitted.
    """
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.lock = threading.Lock()

    def __call__(self, method, **params):
        from hermes_cli.gateway_client import _session_ticket
        from hermes_cli.gateway_runtime_discovery import query_identify
        from websockets.sync.client import connect
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise WorkerPersistenceError('synchronous_rpc_on_event_loop')
        with self.lock:
            descriptor = query_identify(self.home, timeout=5)
            if descriptor.get('pid') == os.getpid():
                raise WorkerPersistenceError('synchronous_self_rpc')
            from hermes_cli.gateway_runtime import _endpoint
            discovery = _endpoint(descriptor, self.home)
            if discovery.state != 'ready' or discovery.endpoint is None:
                raise WorkerPersistenceError('owner_unavailable')
            endpoint = discovery.endpoint
            ticket = _session_ticket(self.home, endpoint,
                purpose='interactive' if method == 'worker.register' else 'worker-adoption')
            url = endpoint.api_origin.replace('https:', 'wss:').replace('http:', 'ws:') + '/api/ws'
            with connect(url, subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket],
                         open_timeout=5, close_timeout=1, max_size=8 * 1024 * 1024) as ws:
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}))
                import time
                deadline = time.monotonic() + 20
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('worker_receipt_timeout')
                    response = json.loads(ws.recv(timeout=remaining))
                    if response.get('id') == 1:
                        break
                if 'error' in response:
                    raise WorkerPersistenceError(response['error'].get('message', 'persistence_failed'))
                return response['result']


class RuntimeSessionStore:
    """Synchronous durable results with a private byte-bounded retry journal.

    A failed write remains pending, and failure is sticky until explicit retry or
    adoption. Local queue acceptance is never returned as canonical commit.
    Pending journals live outside age-pruned cache trees.
    """
    def __init__(self, rpc, scope, outbox_dir, *, max_bytes=4 * 1024 * 1024):
        self.rpc = rpc
        self.scope = dict(scope)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.failure = None
        self.path = Path(outbox_dir) / 'pending.json'
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise WorkerPersistenceError('unsafe_outbox')
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            self.journal = json.loads(self.path.read_text(encoding="utf-8"))
            if self.journal['scope'] != self.scope:
                raise WorkerPersistenceError('outbox_scope_mismatch')
        else:
            self.journal = {'scope': self.scope, 'next_sequence': 1, 'pending': []}
            self._save(self.journal)

    def _save(self, journal):
        encoded = json.dumps(journal, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()
        if len(encoded) > self.max_bytes:
            self.failure = 'outbox_full'
            raise WorkerPersistenceError(self.failure)
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != 'nt':
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _session(self, session_id):
        if session_id != self.scope['session_id']:
            raise WorkerPersistenceError('permission_denied')

    def _apply(self, operation, payload):
        with self.lock:
            if self.failure:
                raise WorkerPersistenceError(self.failure)
            if self.journal['pending']:
                raise WorkerPersistenceError('pending_receipt')
            entry = {'sequence': self.journal['next_sequence'], 'operation': operation, 'payload': payload}
            candidate = json.loads(json.dumps(self.journal))
            candidate['pending'].append(entry)
            candidate['next_sequence'] += 1
            self._save(candidate)
            self.journal = candidate
            return self.retry_pending()[0]

    def retry_pending(self):
        with self.lock:
            results = []
            try:
                while self.journal['pending']:
                    entry = self.journal['pending'][0]
                    result = self.rpc('worker.persist', **self.scope, **entry)
                    candidate = dict(self.journal, pending=self.journal['pending'][1:])
                    self._save(candidate)
                    self.journal = candidate
                    results.append(result)
            except Exception as exc:
                self.failure = str(exc)
                raise
            self.failure = None
            return results

    def adopt(self, epoch):
        """Install only a separately verified worker.adopt result; never self-adopt."""
        with self.lock:
            candidate = dict(self.journal, scope=dict(self.scope, epoch=epoch))
            self._save(candidate)
            self.scope = candidate['scope']
            self.journal = candidate
            self.failure = None

    def append_messages_batch(self, session_id, messages, compression_lock_holder=None,
                              turn_lease_holder=None, chunk_rows=None, turn_lease_ttl_seconds=300.0):
        self._session(session_id)
        if compression_lock_holder is not None or chunk_rows is not None or turn_lease_ttl_seconds != 300.0:
            raise WorkerPersistenceError('unsupported_operation')
        result = self._apply('transcript.append', {'messages': messages, 'turn_lease_holder': turn_lease_holder})
        for message, annotation in zip(messages, result['annotations'], strict=True):
            message.update(annotation)
        return result['count']

    def try_acquire_session_turn_lease(self, session_id, holder, *, ttl_seconds=300.0, patience_s=None):
        self._session(session_id)
        return self._apply('turn.acquire', {'holder': holder, 'ttl_seconds': ttl_seconds})['value']

    def refresh_session_turn_lease(self, session_id, holder, *, ttl_seconds=300.0):
        self._session(session_id)
        return self._apply('turn.renew', {'holder': holder, 'ttl_seconds': ttl_seconds})['value']

    def release_session_turn_lease(self, session_id, holder):
        self._session(session_id)
        self._apply('turn.release', {'holder': holder})

    def queue_token_counts(self, session_id, **usage):
        self._session(session_id)
        self._apply('usage.main', usage)

    def record_auxiliary_usage(self, session_id, task, **usage):
        self._session(session_id)
        self._apply('usage.auxiliary', dict(usage, task=task))

    def flush_token_counts(self, timeout=5.0):
        with self.lock:
            if self.failure:
                raise WorkerPersistenceError(self.failure)
            if self.journal['pending']:
                raise WorkerPersistenceError('pending_receipt')
            return True

    def close(self):
        self.flush_token_counts()
