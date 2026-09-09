"""Concrete worker compression APIs; scope and receipts stay in RuntimeSessionStore."""


class RuntimeSessionCompressionMixin:
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
