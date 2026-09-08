"""Server-stamped grants retain their scope through connection admission."""
from types import SimpleNamespace

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['read-only', 'wrong-profile', 'stale-instance'])
async def test_connection_preserves_server_grant(tmp_path, binding):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = begin_runtime_epoch(db, instance_id='current')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='owned',
                                     instance_id='current', db=db, epoch=epoch)
        authority.sessions['s'] = LiveSession(None, 'route')
        identity = {'user_id': 'human', 'profile_id': 'owned', 'instance_id': 'current',
                    'capabilities': ['session:read']}
        if binding == 'wrong-profile':
            identity['profile_id'] = 'other'
        elif binding == 'stale-instance':
            identity['instance_id'] = 'previous'
        connection = AuthorityConnection(authority, object(), identity)
        try:
            resumed = await connection.dispatch({'id': 1, 'method': 'session.resume',
                                                 'params': {'session_id': 's'}})
            assert ('result' in resumed) == (binding == 'read-only'), resumed
            submitted = await connection.dispatch({'id': 2, 'method': 'prompt.submit',
                                                    'params': {'session_id': 's', 'submission_id': 'denied'}})
            assert submitted['error']['message'] in {'permission_denied', 'profile_mismatch'}, submitted
        finally:
            await connection.close()
