"""Concrete worker compression APIs; scope and receipts stay in RuntimeSessionStore."""


class RuntimeSessionCompressionMixin:
    def get_session(self, session_id):
        if session_id != self.scope['session_id']:
            from agent.runtime_session_store import WorkerPersistenceError
            ids = self._apply('compression.lineage', {'target': self.scope['session_id']})['readable_ids']
            if session_id not in ids:
                raise WorkerPersistenceError('permission_denied')
        return self._apply('compression.context', {'target': session_id})['session']

    def get_compression_lineage(self, session_id):
        return self._apply('compression.lineage', {'target': session_id})['lineage']

    def get_conversation_root(self, session_id):
        return self._apply('compression.lineage', {'target': session_id})['root']

    def get_compression_tip(self, session_id):
        return self._apply('compression.lineage', {'target': session_id})['tip']

    def resolve_resume_session_id(self, session_id):
        return self._apply('compression.lineage', {'target': session_id})['resume']

    def declared_scope_identity(self, session_id):
        return tuple(self._apply('compression.lineage', {'target': session_id})['identity'])

    def is_explicit_fork_child(self, session_id):
        return self.declared_scope_identity(session_id)[0]

    def latest_conversation_boundary(self, session_key, source):
        return self._apply('compression.boundary', {'session_key': session_key, 'source': source})['value']

    def get_messages_as_conversation(self, session_id, include_ancestors=False, include_inactive=False,
                                     repair_alternation=False, include_row_ids=False, include_compacted=False):
        return self._apply('compression.history', dict(target=session_id, include_ancestors=include_ancestors,
            include_inactive=include_inactive, repair_alternation=repair_alternation,
            include_row_ids=include_row_ids, include_compacted=include_compacted))['messages']

    def try_acquire_compression_lock(self, session_id, holder, ttl_seconds=300.0):
        self._session(session_id)
        return self._apply('compression.lock.acquire', {'holder': holder, 'ttl_seconds': ttl_seconds})['value']

    def refresh_compression_lock(self, session_id, holder, ttl_seconds=300.0):
        self._session(session_id)
        return self._apply('compression.lock.renew', {'holder': holder, 'ttl_seconds': ttl_seconds})['value']

    def release_compression_lock(self, session_id, holder):
        self._session(session_id)
        self._apply('compression.lock.release', {'holder': holder})

    def get_compression_lock_holder(self, session_id):
        self._session(session_id)
        return self._apply('compression.lock.holder', {})['value']

    def record_compression_failure_cooldown(self, session_id, cooldown_until, error=None):
        self._session(session_id)
        self._apply('compression.cooldown.record', {'cooldown_until': cooldown_until, 'error': error})

    def restore_compression_failure_cooldown_row(self, session_id, snapshot):
        self._session(session_id)
        self._apply('compression.cooldown.restore', {'snapshot': snapshot})

    def clear_compression_failure_cooldown(self, session_id):
        self._session(session_id)
        self._apply('compression.cooldown.clear', {})

    def set_compression_fallback_streak(self, session_id, streak):
        self._session(session_id)
        self._apply('compression.fallback', {'value': max(0, int(streak))})

    def set_compression_ineffective_count(self, session_id, count):
        self._session(session_id)
        self._apply('compression.ineffective', {'value': max(0, int(count))})

    def set_compression_recovery_deadline(self, session_id, deadline):
        self._session(session_id)
        try:
            value = max(0.0, float(deadline or 0.0))
        except (TypeError, ValueError):
            value = 0.0
        self._apply('compression.recovery', {'value': value or None})
