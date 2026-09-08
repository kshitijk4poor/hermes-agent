import pytest

from hermes_state import SessionDB
import hermes_state_runtime as rt


@pytest.mark.parametrize('recover_first', [False, True])
def test_explicit_worker_adoption_preserves_claim_without_reexecution(tmp_path, recover_first):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('s', source='test')
        epoch = rt.begin_runtime_epoch(db, instance_id='boot')
        accepted = rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s', request_id='input', payload={'text': 'work'})
        claim = rt.claim_session_input(db, epoch=epoch, session_id='s')
        assignment = dict(execution_id='compute', session_id='s', generation=claim['generation'])
        rt.register_worker_execution(db, epoch=epoch, **assignment, kind='compute', adoption_secret='private')
        new_epoch = rt.begin_runtime_epoch(db, instance_id='replacement')
        if recover_first:
            rt.recover_session_inputs(db, epoch=new_epoch)
        rt.adopt_worker_execution(db, epoch=new_epoch, **assignment, adoption_secret='private')
        rt.recover_session_inputs(db, epoch=new_epoch)
        restored = rt.get_session_admission(db, admission_id=accepted['admission_id'])
        assert restored['status'] == 'started' and restored['owner_epoch'] == new_epoch
        assert restored['generation'] == claim['generation']
        assert rt.claim_session_input(db, epoch=new_epoch, session_id='s') is None
        result = rt.settle_session_input(db, epoch=new_epoch, admission_id=accepted['admission_id'], generation=claim['generation'], outcome='completed')
        assert result['status'] == 'terminal'
        with pytest.raises(rt.RuntimeStoreError, match='stale_generation'):
            rt.persist_worker_message(db, epoch=new_epoch, **assignment, sequence=1, role='assistant', content='late')
    finally:
        db.close()
