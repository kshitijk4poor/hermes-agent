"""Closed worker lifecycle handlers, called only inside the receipt transaction."""
import json
import time

from hermes_state_runtime import RuntimeStoreError


_IDENTITY_FIELDS = frozenset({
    'user_id', 'session_key', 'chat_id', 'chat_type', 'thread_id',
    'parent_session_id', 'cwd', 'profile_name', 'git_repo_root', 'origin_json', 'display_name',
})
_CREATE_FIELDS = _IDENTITY_FIELDS | {'source', 'model', 'model_config', 'system_prompt'}
_CONSTRUCTOR_CONFIG = frozenset({'max_iterations', 'max_tokens', 'reasoning_config', 'yolo_mode'})


def worker_create(db, conn, session_id, payload):
    if set(payload) - _CREATE_FIELDS or 'source' not in payload:
        raise RuntimeStoreError('invalid_params')
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    # A worker cannot mint a row or supply unreserved routing, lineage or cwd.
    if row is None or payload['source'] != row['source']:
        raise RuntimeStoreError('permission_denied')
    for key in _IDENTITY_FIELDS:
        value = payload.get(key)
        if value is not None and value != row[key]:
            raise RuntimeStoreError('permission_denied')
    for key in _CREATE_FIELDS - {'model_config'}:
        if payload.get(key) is not None and not isinstance(payload[key], str):
            raise RuntimeStoreError('invalid_params')
    config = payload.get('model_config')
    if config is not None and (not isinstance(config, dict) or set(config) - _CONSTRUCTOR_CONFIG):
        raise RuntimeStoreError('invalid_params')
    insert_session_row_in_transaction(db, conn, session_id, **payload)
    return {'value': session_id}


def worker_end(db, conn, session_id, payload):
    if set(payload) != {'end_reason'} or not isinstance(payload['end_reason'], str) or not payload['end_reason']:
        raise RuntimeStoreError('invalid_params')
    db._end_and_bump(conn,
        'UPDATE sessions SET ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL',
        (time.time(), payload['end_reason'], session_id), session_id, payload['end_reason'])
    return {'value': None}


def worker_lifecycle(db, conn, session_id, payload):
    from hermes_state_sessions import classify_session_status
    if payload:
        raise RuntimeStoreError('invalid_params')
    row = conn.execute("SELECT role,tool_calls IS NOT NULL AS has_tool_calls,finish_reason "
                       "FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    value = classify_session_status(role=row['role'], has_tool_calls=bool(row['has_tool_calls']),
                                    finish_reason=row['finish_reason']) if row else 'empty'
    return {'value': value}


WORKER_LIFECYCLE_HANDLERS = {
    'session.create': worker_create,
    'session.end': worker_end,
    'session.lifecycle': worker_lifecycle,
}


def insert_session_row_in_transaction(
        self, conn, session_id, source, model=None, model_config=None,
        system_prompt=None, user_id=None, session_key=None, chat_id=None,
        chat_type=None, thread_id=None, parent_session_id=None, cwd=None,
        profile_name=None, git_repo_root=None, origin_json=None, display_name=None):
    """Connection-taking body of the keep-existing constructor upsert.

    The ordinary constructor should delegate here too. Worker identity is
    validated before entry; this helper never opens or commits a DB.
    """
    from hermes_state_sessions import _UPSERT_KEEP_EXISTING_SQL
    if not (profile_name or "").strip():
        profile_name = self._own_profile_name()
    system_prompt_hash = self._store_system_prompt(conn, system_prompt)
    conn.execute(
        """INSERT INTO sessions (
           id, source, user_id, session_key, chat_id, chat_type, thread_id,
           model, model_config, system_prompt, system_prompt_hash,
           parent_session_id, cwd, profile_name, git_repo_root,
           origin_json, display_name, started_at
        )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               model = COALESCE(sessions.model, excluded.model),
               model_config = CASE
                   WHEN excluded.model_config IS NOT NULL
                        AND json_type(
                            sessions.model_config, '$._reset_from'
                        ) IS NOT NULL
                        AND json_remove(
                            sessions.model_config, '$._reset_from'
                        ) = '{}'
                   THEN json_set(
                       excluded.model_config,
                       '$._reset_from',
                       json_extract(
                           sessions.model_config, '$._reset_from'
                       )
                   )
                   ELSE COALESCE(
                       sessions.model_config, excluded.model_config
                   )
               END,
               system_prompt_hash = COALESCE(
                   sessions.system_prompt_hash,
                   excluded.system_prompt_hash
               ),
               system_prompt = CASE
                   WHEN sessions.system_prompt_hash IS NULL
                        AND excluded.system_prompt_hash IS NOT NULL
                   THEN NULL
                   ELSE sessions.system_prompt
               END,
""" + _UPSERT_KEEP_EXISTING_SQL,
        (
            session_id, source, user_id, session_key, chat_id, chat_type, thread_id, model,
            json.dumps(model_config) if model_config else None, system_prompt_hash,
            parent_session_id, cwd, profile_name, git_repo_root, origin_json, display_name,
            time.time(),
        ),
    )
    if system_prompt_hash is not None:
        self._delete_unreferenced_system_prompts(conn)
    if parent_session_id:
        self._inherit_parent_session_metadata(conn, session_id)
