"""Action-specific authority mutations, shared by WS and authenticated HTTP.

No legacy slash handler is executed after a receipt: those handlers own separate
transactions. Reset/branch/compress/model need prepared runtime publication and
remain explicitly unavailable until that contract is implemented.
"""
from hermes_state_runtime import RuntimeStoreError, mutate_runtime_session

_METADATA = frozenset({'rename', 'archive', 'sidebar'})
_RUNTIME_ACTIONS = frozenset({'reset', 'branch', 'compress', 'model'})
_FIELDS = frozenset({'session_id', 'request_id', 'expected_revision', 'operation', 'payload'})


async def mutate_session(authority, actor, ref, params):
    if (set(params) - {'expected_generation'} != _FIELDS
            or params['session_id'] != ref.session_id):
        raise RuntimeStoreError('invalid_params')
    operation = params['operation']
    if not isinstance(operation, str):
        raise RuntimeStoreError('invalid_params')
    if operation == 'import':
        if actor.profile_id != authority.profile_id or ref.profile_id != authority.profile_id:
            raise RuntimeStoreError('profile_mismatch')
        if 'session:create' not in actor.capabilities:
            raise RuntimeStoreError('permission_denied')
    else:
        authority.authorize(actor, ref, 'session:control')
    authority._require_admission_open()
    live = authority.sessions.get(ref.session_id)

    applied = False

    def live_guard(targets):
        nonlocal applied
        applied = True
        if operation in _METADATA or operation == 'import':
            return
        for sid in targets:
            candidate = authority.sessions.get(sid)
            if candidate is not None and candidate.task is not None and not candidate.task.done():
                raise RuntimeStoreError('session_busy')
        if operation == 'delete':
            # Route retirement must commit with deletion, not recreate the same
            # physical ID on the next native message. Do not fake that handoff.
            store = getattr(authority.runner, 'session_store', None)
            if store is not None and any(store.lookup_by_session_id(sid) is not None for sid in targets):
                raise RuntimeStoreError('runtime_coordination_required')
        if operation == 'rewind' and not callable(getattr(authority.runner, '_evict_cached_agent', None)):
            raise RuntimeStoreError('runtime_coordination_required')

    if operation in _RUNTIME_ACTIONS:
        live_guard([ref.session_id])
        raise RuntimeStoreError('runtime_coordination_required')
    result = mutate_runtime_session(authority.db, epoch=authority.epoch,
        principal_id=actor.subject, session_id=ref.session_id, request_id=params['request_id'],
        expected_revision=params['expected_revision'], expected_generation=params.get('expected_generation'),
        operation=operation, payload=params['payload'], _live_guard=live_guard)
    if applied and live is not None:
        if operation == 'rewind':
            authority.runner._evict_cached_agent(live.route)
        live.event_stream.publish(ref.session_id, result, event_type='session.updated')
    return result
