"""Control-only ownership for unbound legacy history; never an execution policy."""
import json
from hermes_state_runtime import RuntimeStoreError

BINDING_PREFIX = 'gateway.history_control.v1.'


def authorize_history(conn, actor, session_id, *, claim=False):
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row is None or row['source'] not in {'cli', 'tui', 'gui', 'import'}:
        return False
    # Import intentionally strips routes. Never reinterpret a cold native/API
    # transcript or another user's owned local conversation as imported history.
    if row['session_key'] or row['chat_id'] or row['origin_json']:
        return False
    if row['user_id'] and row['user_id'] != actor.subject:
        raise RuntimeStoreError('permission_denied')
    if 'session:create' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    key = BINDING_PREFIX + session_id
    binding = json.dumps([actor.profile_id, actor.subject], separators=(',', ':'))
    saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    if saved is not None and saved[0] != binding:
        raise RuntimeStoreError('permission_denied')
    if claim and saved is None:
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, binding))
    return True
