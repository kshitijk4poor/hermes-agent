"""Explicit lifecycle methods for the assignment-bound worker store.

The owner reserves identity before construction. Closing the local store remains
separate from ending its session and settling execution (including deferred work).
"""


class RuntimeSessionLifecycleMixin:
    def create_session(self, session_id, source, *, model=None, model_config=None,
                       system_prompt=None, user_id=None, session_key=None, chat_id=None,
                       chat_type=None, thread_id=None, parent_session_id=None, cwd=None,
                       profile_name=None, git_repo_root=None, origin_json=None, display_name=None):
        self._session(session_id)
        payload = dict(source=source, model=model, model_config=model_config, system_prompt=system_prompt,
                       user_id=user_id, session_key=session_key, chat_id=chat_id, chat_type=chat_type,
                       thread_id=thread_id, parent_session_id=parent_session_id, cwd=cwd,
                       profile_name=profile_name, git_repo_root=git_repo_root,
                       origin_json=origin_json, display_name=display_name)
        return self._apply('session.create', payload)['value']

    def end_session(self, session_id, end_reason):
        self._session(session_id)
        self._apply('session.end', {'end_reason': end_reason})

    def session_lifecycle_statuses(self, session_ids):
        ids = [sid for sid in (session_ids or []) if sid]
        for sid in ids:
            self._session(sid)
        if not ids:
            return {}
        return {self.scope['session_id']: self._apply('session.lifecycle', {})['value']}
