"""Runtime-bound API storage selection; borrowed stores are never adapter-owned."""
from pathlib import Path


def selected_session_db(adapter, home):
    authority = getattr(adapter.gateway_runner, 'session_authority', None)
    if authority is None:
        return None
    if Path(authority.db.db_path).parent.resolve() != Path(home).resolve():
        # A multiplex route is not evidence that this runtime owns that profile.
        return None
    with adapter._session_db_cache_lock:
        if adapter._session_db_cache_closed:
            return None
    return authority.db
