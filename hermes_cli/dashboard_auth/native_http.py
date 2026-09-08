"""Same-user private bootstrap at the existing HTTP authentication boundary.

This is not remote/exposure delegation. The daemon captures the actual socket
peer before proxy middleware, and the private control channel is the credential
issuer. A browser Origin (including an empty one) always disqualifies this path.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException
from starlette.responses import JSONResponse

HEADER = 'x-hermes-gateway-ticket'


def _own_profile(value, profile_id, *, current=True):
    from hermes_cli.web_server_profiles import _resolve_profile_dir

    if value is None or (isinstance(value, str) and current
                         and (not value.strip() or value.strip().lower() == 'current')):
        return
    if not isinstance(value, str):
        raise PermissionError('profile_scope_mismatch')
    try:
        target = _resolve_profile_dir(value.strip()).resolve()
    except HTTPException:
        raise PermissionError('profile_scope_mismatch') from None
    if target != Path(profile_id):
        raise PermissionError('profile_scope_mismatch')


async def _check_profile_scope(request, profile_id):
    # Existing handlers select body.profile OR query.profile. Check every value,
    # including repeated query keys, rather than normalizing away a disagreement.
    path = request.url.path.rstrip('/')
    current = not path.startswith(('/api/profiles/', '/api/cron/jobs', '/api/sessions',
                                   '/api/fs/download', '/api/fs/read-data-url'))
    for value in request.query_params.getlist('profile'):
        _own_profile(value, profile_id, current=current)
    content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if not content_type or content_type == 'application/json' or (
            content_type.startswith('application/') and content_type.endswith('+json')):
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None  # Route validation still owns malformed JSON.
        if isinstance(body, dict) and 'profile' in body:
            _own_profile(body['profile'], profile_id, current=current)
    if path.startswith('/api/cron/jobs') and not request.query_params.get('profile'):
        # Job-id discovery and the default list scan other profiles. Require an
        # explicit, verified named profile instead of silently changing defaults.
        raise PermissionError('profile_scope_mismatch')
    if path == '/api/profiles':
        # GET is the existing discovery list, not authority to open their DBs.
        if request.method != 'GET':
            raise PermissionError('profile_scope_mismatch')
    elif path.startswith('/api/profiles/'):
        suffix = path.removeprefix('/api/profiles/')
        aggregate_selector = {
            'sessions': 'profile', 'sessions/sidebar': 'recents_profile',
        }.get(suffix)
        if aggregate_selector:
            values = request.query_params.getlist(aggregate_selector)
            if not values:
                raise PermissionError('profile_scope_mismatch')
            for value in values:
                # These routes interpret absent/all differently from /api/config.
                _own_profile(value, profile_id, current=False)
        elif suffix == 'active' and request.method == 'GET':
            return  # Discovery of sticky defaults does not switch this daemon.
        elif suffix in {'active', 'import', 'projects/tree', 'sessions/pull-requests'}:
            raise PermissionError('profile_scope_mismatch')
        else:
            _own_profile(suffix.split('/', 1)[0], profile_id, current=False)
            if request.method in {'PATCH', 'DELETE'}:
                # Moving/deleting the reserved home is not an in-place operation.
                raise PermissionError('profile_scope_mismatch')


async def authenticate_native_http(request):
    """Return a rejection or stamp a one-request principal; absent header is inert."""
    tickets = request.headers.getlist(HEADER)
    if not tickets:
        return None
    runner = getattr(request.app.state, 'gateway_runner', None)
    descriptor = getattr(runner, 'session_runtime_descriptor', {})
    authority = getattr(runner, 'session_authority', None)
    store = getattr(runner, 'session_ticket_store', None)
    peer = request.scope.get('hermes.gateway_socket_peer')
    if (len(tickets) != 1 or 'origin' in request.headers or not peer
            or peer[0] not in {'127.0.0.1', '::1'}
            or descriptor.get('state') != 'ready' or getattr(runner, '_draining', True)
            or authority is None or store is None
            or store.instance_id != descriptor.get('instance_id')
            or authority.instance_id != store.instance_id
            or descriptor.get('served_profiles') != [
                {'profile_id': authority.profile_id, 'home': authority.profile_id}]):
        return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
    try:
        grant = store.redeem(tickets[0], profile_id=authority.profile_id, purpose='native-http')
        if grant['capabilities'] != frozenset({'http:owner'}):
            raise PermissionError('invalid native grant')
    except PermissionError:
        return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
    try:
        await _check_profile_scope(request, grant['profile_id'])
    except PermissionError:
        return JSONResponse({'detail': 'profile_scope_mismatch'}, status_code=403)
    request.state.native_http_principal = grant
    return None
