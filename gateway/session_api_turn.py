"""Trusted API preparation and observation of the canonical durable FIFO."""
import asyncio
from contextvars import ContextVar
import json
import uuid

from gateway.config import Platform
from gateway.session_api import bind_api_session, restore_api_session
from gateway.session_results import admission_result
from hermes_state_runtime import RuntimeStoreError, admit_session_input, _epoch, _json

api_execution: ContextVar[dict | None] = ContextVar('api_execution', default=None)
_SETTINGS_PREFIX = 'gateway.api.settings.v1.'
_SETTING_KEYS = ('ephemeral_system_prompt', 'requested_model', 'requested_provider',
                 'model_options', 'route', 'session_model', 'confirmed_runtime_lock',
                 'requested_runtime', 'route_source')


def api_settings(authority, ref):
    with authority.db._read_ctx() as conn:
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_SETTINGS_PREFIX + ref.session_id,)).fetchone()
    return json.loads(saved[0]) if saved else {}


def check_api_turn(authority, ref, payload):
    live = authority.sessions[ref.session_id]
    if live.source.platform == Platform.API_SERVER:
        restore_api_session(authority, ref.session_id)
    adapter = authority.runner._adapter_for_source(live.source)
    if adapter is None or getattr(adapter, 'gateway_runner', None) is not authority.runner:
        raise RuntimeStoreError('runtime_draining')
    if 'api_turn_v1' in payload:
        data = payload['api_turn_v1']
        if set(data) != {'history', 'settings'} or not isinstance(data['history'], list):
            raise RuntimeStoreError('invalid_params')
        if set(data['settings']) - set(_SETTING_KEYS):
            raise RuntimeStoreError('invalid_params')
    return adapter


async def run_api_turn(adapter, **kwargs):
    authority = adapter.gateway_runner.session_authority
    if adapter._ensure_session_db() is not authority.db:
        raise RuntimeStoreError('profile_mismatch')
    sid = kwargs.get('session_id') or uuid.uuid4().hex
    ref = bind_api_session(authority, sid)
    settings = {key: kwargs.get(key) for key in _SETTING_KEYS}
    # Route credentials remain in the server's configuration, never admission JSON.
    route = settings.get('route')
    if route and route.get('api_key'):
        alias = settings.get('requested_model')
        configured = adapter._model_routes.get(alias)
        if configured != route:
            raise RuntimeStoreError('permission_denied')
        settings['route'] = {k: v for k, v in route.items() if k != 'api_key'}
    payload = json.loads(_json({'text': kwargs['user_message'], 'api_turn_v1': {
        'history': kwargs['conversation_history'], 'settings': settings}}))
    check_api_turn(authority, ref, payload)
    row = admit_session_input(authority.db, epoch=authority.epoch, principal_id='api',
                              session_id=sid, request_id=kwargs.get('active_run_id') or uuid.uuid4().hex,
                              payload=payload)
    if row['status'] == 'terminal':
        result = admission_result(authority.db, row['admission_id'])
        if result is None:
            raise RuntimeStoreError('unknown_execution')
        return result['result'], result['usage']
    waiter = authority.waiters.setdefault(row['admission_id'], asyncio.get_running_loop().create_future())
    authority._publish_pending(ref)
    authority._schedule(ref)
    await asyncio.shield(waiter)
    saved = admission_result(authority.db, row['admission_id'])
    if saved is None:
        raise RuntimeStoreError('unknown_execution')
    return saved['result'], saved['usage']


def prepare_api_execution(authority, ref, payload):
    adapter = check_api_turn(authority, ref, payload)
    data = payload.get('api_turn_v1')
    settings = data['settings'] if data else api_settings(authority, ref)
    if data:
        def write(conn):
            _epoch(conn, authority.epoch)
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?) '
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (_SETTINGS_PREFIX + ref.session_id, _json(settings)))
        authority.db._execute_write(write)
    return {'adapter': adapter, 'settings': settings, 'history': data['history'] if data else None}


def prepare_api_runtime(model, runtime_kwargs):
    current = api_execution.get()
    if current is None:
        return model, runtime_kwargs
    options = current['settings']
    route = options.get('route')
    configured = current['adapter']._model_routes.get(options.get('requested_model'))
    if configured and {k: v for k, v in configured.items() if k != 'api_key'} == route:
        route = configured
    model, _, _, _ = current['adapter']._select_agent_runtime(runtime_kwargs, model,
        requested_model=options.get('requested_model'), requested_provider=options.get('requested_provider'),
        route=route, session_model=options.get('session_model'),
        confirmed_runtime_lock=bool(options.get('confirmed_runtime_lock')),
        gateway_session_key=None, session_id=None)
    return model, runtime_kwargs
