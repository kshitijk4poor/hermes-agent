"""Competing viewers must not overwrite a newer metadata revision."""
from types import SimpleNamespace

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.mark.asyncio
async def test_mutation_rpc_fences_retries_and_competing_viewers(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = begin_runtime_epoch(db, instance_id='current')
        runner = SimpleNamespace(_draining=False)
        authority = SessionAuthority(runner, profile_id='owned', instance_id='current', db=db, epoch=epoch)
        authority.sessions['s'] = LiveSession(None, 'route')
        owner = AuthorityConnection(authority, object(), {'user_id': 'owner'})
        viewer = AuthorityConnection(authority, object(), {'user_id': 'reader', 'capabilities': ['session:read']})
        request = {'id': 1, 'method': 'session.mutate', 'params': {
            'session_id': 's', 'request_id': 'rename-1', 'expected_revision': db.get_session('s')['runtime_revision'],
            'operation': 'rename', 'payload': {'title': 'Owned title'}}}
        try:
            denied = await viewer.dispatch(request)
            assert denied['error']['message'] == 'permission_denied', denied
            first = await owner.dispatch(request)
            assert first['result']['title'] == 'Owned title', first
            revision = db.get_session('s')['runtime_revision']
            assert await owner.dispatch(request) == first
            assert db.get_session('s')['runtime_revision'] == revision
            competing = {**request, 'params': {**request['params'], 'request_id': 'rename-2',
                                                'payload': {'title': 'Stale title'}}}
            conflict = await owner.dispatch(competing)
            assert conflict['error']['message'] == 'revision_conflict', conflict
            assert db.get_session('s')['title'] == 'Owned title'
            archived = await owner.dispatch({**request, 'params': {**request['params'],
                'request_id': 'archive-1', 'expected_revision': revision,
                'operation': 'archive', 'payload': {'archived': True}}})
            assert archived['result']['archived'] is True, archived
            runner._draining = True
            stopped = await owner.dispatch(request)
            assert stopped['error']['message'] == 'runtime_draining', stopped
        finally:
            await owner.close()
            await viewer.close()
