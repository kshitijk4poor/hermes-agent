"""Constructor context is assignment-scoped and shares durable mutation receipts."""
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution


def test_worker_context_preserves_authority_identity_and_prompt_receipts(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('owned', 'subagent', parent_session_id=None, model='requested',
                          model_config={'_delegate_from': 'parent'}, system_prompt='prefix', profile_name='fixture')
        db.create_session('foreign', 'cli', system_prompt='private')
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
        register_worker_execution(db, **scope, kind='child', adoption_secret='test-secret')
        store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
        try:
            assert store.get_session('owned')['system_prompt'] == 'prefix'
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.get_session('foreign')
            store.update_system_prompt('owned', 'stable-prefix')
            store.patch_session_model_config('owned', {'_usage_anchor': {'tokens': 23}})
            assert store.get_session_model_config_value('owned', '_usage_anchor') == {'tokens': 23}
            assert db.get_session_model_config_value('owned', '_delegate_from') == 'parent'
            assert db.get_session('foreign')['system_prompt'] == 'private'
            original = store.rpc
            def lost_ack(method, **params):
                original(method, **params)
                raise TimeoutError('lost-ack')
            store.rpc = lost_ack
            with pytest.raises(TimeoutError, match='lost-ack'):
                store.update_system_prompt('owned', 'durable-prefix')
            store.rpc = original
            assert len(store.retry_pending()) == 1
            assert db.get_session('owned')['system_prompt'] == 'durable-prefix'
            with pytest.raises(Exception, match='invalid_params'):
                mutate_worker_execution(db, **scope, sequence=store.journal['next_sequence'],
                                        operation='session.sidecars', payload={'patch': {'_delegate_from': 'foreign'}})
        finally:
            store.close()
    finally:
        db.close()
