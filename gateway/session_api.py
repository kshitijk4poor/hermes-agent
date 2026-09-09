"""Server-only binding of API transcript identities to the existing TurnRunner."""
import json
from datetime import datetime, timezone

from gateway.config import Platform
from gateway.session import SessionEntry, SessionSource, _is_path_unsafe
from gateway.session_contract import SessionRef
from hermes_state_runtime import RuntimeStoreError, _epoch, _json

_BINDING_PREFIX = 'gateway.api.binding.v1.'


def bind_api_session(authority, session_id):
    """Only the authenticated API edge may reserve an API source; never public RPC."""
    authority._require_admission_open()
    if not isinstance(session_id, str) or not session_id or _is_path_unsafe(session_id):
        raise RuntimeStoreError('invalid_params')
    if session_id in authority.sessions:
        if authority.sessions[session_id].source.platform != Platform.API_SERVER:
            raise RuntimeStoreError('permission_denied')
        return SessionRef(authority.profile_id, session_id)
    source = SessionSource(platform=Platform.API_SERVER, chat_id=session_id,
                           user_id='api', chat_type='dm')
    route = authority.runner.session_store._generate_session_key(source)
    now = datetime.now(timezone.utc)
    entry = SessionEntry(route, session_id, now, now, origin=source, platform=Platform.API_SERVER)
    receipt = {'profile_id': authority.profile_id, 'session_id': session_id,
               'route': route, 'entry': entry.to_dict()}

    def write(conn):
        _epoch(conn, authority.epoch)
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_BINDING_PREFIX + session_id,)).fetchone()
        if saved is not None:
            return
        row = conn.execute('SELECT source,session_key FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is not None and row['source'] != 'api_server':
            raise RuntimeStoreError('permission_denied')
        if row is not None and row['session_key'] not in (None, '', route):
            raise RuntimeStoreError('admission_conflict')
        existing = conn.execute("SELECT entry_json FROM gateway_routing WHERE scope='' AND session_key=?",
                                (route,)).fetchone()
        if existing is not None and json.loads(existing[0])['session_id'] != session_id:
            raise RuntimeStoreError('admission_conflict')
        conn.execute('''INSERT INTO sessions(id,source,started_at) VALUES(?,'api_server',?)
                        ON CONFLICT(id) DO NOTHING''', (session_id, now.timestamp()))
        conn.execute('UPDATE sessions SET session_key=?,chat_id=?,user_id=?,chat_type=?,origin_json=? WHERE id=?',
                     (route, session_id, source.user_id, 'dm', _json(source.to_dict()), session_id))
        conn.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES('',?,?,?) "
                     'ON CONFLICT(scope,session_key) DO UPDATE SET entry_json=excluded.entry_json',
                     (route, _json(entry.to_dict()), now.timestamp()))
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     (_BINDING_PREFIX + session_id, _json(receipt)))
    authority.db._execute_write(write)
    return restore_api_session(authority, session_id)


def restore_api_session(authority, session_id):
    from gateway.session_authority import LiveSession
    with authority.db._read_ctx() as conn:
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_BINDING_PREFIX + session_id,)).fetchone()
    if saved is None:
        raise RuntimeStoreError('not_found')
    receipt = json.loads(saved[0])
    if receipt['profile_id'] != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    entry = SessionEntry.from_dict(receipt['entry'])
    source = entry.origin
    row = authority.db.get_session(session_id)
    if (receipt['session_id'] != session_id or entry.session_id != session_id
            or source.platform != Platform.API_SERVER or source.chat_id != session_id
            or row is None or row['source'] != 'api_server'
            or (row['session_key'], row['chat_id'], row['user_id']) != (entry.session_key, session_id, source.user_id)
            or entry.session_key != authority.runner.session_store._generate_session_key(source)):
        raise RuntimeStoreError('admission_conflict')
    store = authority.runner.session_store
    with store._lock:
        store._ensure_loaded_locked()
        current = store._entries.get(entry.session_key)
        if current is not None and current.session_id != session_id:
            raise RuntimeStoreError('admission_conflict')
        store._entries[entry.session_key] = entry
    authority.sessions.setdefault(session_id, LiveSession(source, entry.session_key))
    return SessionRef(authority.profile_id, session_id)
