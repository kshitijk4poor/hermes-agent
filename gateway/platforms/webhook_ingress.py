"""Receipt-only adapter ingress: the authority, not an HTTP/WS task, owns execution."""
from contextlib import asynccontextmanager

from hermes_state_runtime import RuntimeStoreError


@asynccontextmanager
async def producer_scope(adapter, event):
    from gateway.run import _async_profile_runtime_scope
    from gateway.session_ingress_context import native_callback

    runner = getattr(adapter._message_handler, '__self__', None)
    authority = getattr(runner, 'session_authority', None)
    if authority is None:
        raise RuntimeStoreError('not_found')
    registered, profile = runner._owning_profile(adapter, adapter.platform)
    home = getattr(runner, '_native_transport_homes', {}).get(profile)
    if not registered or home is None:
        raise RuntimeStoreError('profile_mismatch')
    source = event.source
    if profile:
        runner._stamp_event_profile(event, profile)
    elif not source.profile and not runner._stamp_routed_profile(source):
        source.profile_route_rejected = True
    source._authorization_profile_home = home
    runtime_home = runner._resolve_profile_home_for_source(source) if source.profile else home
    with native_callback(runner, event, home, profile):
        async with _async_profile_runtime_scope(runtime_home):
            yield authority


async def admit_producer(adapter, event):
    async with producer_scope(adapter, event) as authority:
        return await authority.admit_native(event)
