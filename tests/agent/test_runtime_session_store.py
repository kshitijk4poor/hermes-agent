import json

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


def test_outbox_freezes_failed_payload_and_reopens_exclusively(tmp_path):
    scope = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}
    rows = [{'role': 'user', 'content': 'frozen'}]
    def unavailable(method, **params):
        rows[0]['content'] = 'caller changed'
        raise TimeoutError('offline')
    store = RuntimeSessionStore(unavailable, scope, tmp_path / 'private')
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch('s', rows)
        disk = json.loads(store.path.read_text())
        assert disk['pending'][0]['payload']['messages'][0]['content'] == 'frozen'
        assert store.journal['pending'] == disk['pending']
        with pytest.raises(WorkerPersistenceError, match='outbox_in_use'):
            RuntimeSessionStore(unavailable, scope, tmp_path / 'private')
        with pytest.raises(WorkerPersistenceError, match='offline'):
            store.queue_token_counts('s', input_tokens=1)
    finally:
        with pytest.raises(WorkerPersistenceError, match='offline'):
            store.close()
    seen = []
    def commit(method, **params):
        seen.append(params)
        return {'count': 1, 'annotations': [{'_row_id': 42}]}
    restored = RuntimeSessionStore(commit, scope, tmp_path / 'private')
    try:
        assert restored.retry_pending()[0]['count'] == 1
        assert seen[0]['payload']['messages'][0]['content'] == 'frozen'
        assert json.loads(restored.path.read_text())['pending'] == []
        assert restored.path.stat().st_mode & 0o077 == 0
    finally:
        restored.close()


def test_outbox_capacity_failure_does_not_advance_or_discard(tmp_path):
    def unavailable(*args, **kwargs):
        raise TimeoutError('offline')
    store = RuntimeSessionStore(unavailable, {'session_id': 's'}, tmp_path / 'private', max_bytes=500)
    try:
        before = store.path.read_bytes()
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'x' * 1000}])
        assert store.path.read_bytes() == before
        assert store.failure == 'outbox_full'
    finally:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.close()


def test_worker_turn_wait_callbacks_remain_local(tmp_path):
    attempts, notices = [], []
    def rpc(method, **params):
        attempts.append(params)
        return {'value': len(attempts) > 1}
    store = RuntimeSessionStore(rpc, {'session_id': 's'}, tmp_path / 'private')
    try:
        assert store.acquire_session_turn_lease('s', 'holder', wait_seconds=3,
            poll_interval_seconds=.01, on_wait=notices.append)
        assert notices and len(attempts) == 2
        assert all(set(row['payload']) == {'holder', 'ttl_seconds'} for row in attempts)
        assert not store.acquire_session_turn_lease('s', 'holder', should_abort=lambda: True)
    finally:
        store.close()


def test_outbox_filesystem_failure_is_sticky(tmp_path, monkeypatch):
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    def fail_replace(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('agent.runtime_session_store.os.replace', fail_replace)
    try:
        with pytest.raises(OSError, match='disk unavailable'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.failure == 'disk unavailable'
    finally:
        store._outbox_owner.close()
