"""Process-local callback provenance; private snapshots are revalidated, not grants."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import weakref

from hermes_state_runtime import RuntimeStoreError

_callback = ContextVar('native_ingress_callback', default=None)


@contextmanager
def native_callback(runner, event, transport_home):
    token = _callback.set((runner, event, Path(transport_home).resolve()))
    try:
        yield
    finally:
        _callback.reset(token)


def capture_provenance(runner, event):
    context = _callback.get()
    if context is None or context[0] is not runner or context[1] is not event:
        return None
    source = event.source
    owner = runner._transport_owner(source)
    if owner is None or owner[1] is not None:
        raise RuntimeStoreError('not_found')
    adapter = owner[0]
    home = context[2]
    if Path(runner.session_authority.db.db_path).resolve().parent != home:
        raise RuntimeStoreError('profile_mismatch')
    return {'transport_home': str(home), 'runtime_home': str(home),
            'platform': source.platform.value,
            'connector': runner._adapter_credential_fingerprint(adapter)}


def restore_provenance(runner, source, provenance):
    """Resolve current owned connector/home before installing in-process auth context."""
    home = Path(runner.session_authority.db.db_path).resolve().parent
    adapter = runner.adapters.get(source.platform)
    if adapter is None:
        raise RuntimeStoreError('not_found')
    expected = {'transport_home': str(home), 'runtime_home': str(home),
                'platform': source.platform.value,
                'connector': runner._adapter_credential_fingerprint(adapter)}
    if provenance != expected:
        raise RuntimeStoreError('profile_mismatch')
    source._transport_adapter_ref = weakref.ref(adapter)
    source._authorization_profile_home = home
    return home


def callback_runner():
    context = _callback.get()
    return context[0] if context else None
