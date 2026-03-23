"""Persistent multi-credential pool for same-provider failover."""

from __future__ import annotations

import time
import uuid
import os
from dataclasses import dataclass, fields
from typing import Any, Dict, List, Optional

from hermes_constants import OPENROUTER_BASE_URL
import hermes_cli.auth as auth_mod
from hermes_cli.auth import (
    ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    DEFAULT_AGENT_KEY_MIN_TTL_SECONDS,
    PROVIDER_REGISTRY,
    _agent_key_is_usable,
    _codex_access_token_is_expiring,
    _decode_jwt_claims,
    _is_expiring,
    _load_auth_store,
    _load_provider_state,
    read_credential_pool,
    write_credential_pool,
)

EXHAUSTED_TTL_SECONDS = 24 * 60 * 60


@dataclass
class PooledCredential:
    provider: str
    id: str
    label: str
    auth_type: str
    priority: int
    source: str
    access_token: str
    refresh_token: Optional[str] = None
    last_status: Optional[str] = None
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    base_url: Optional[str] = None
    expires_at: Optional[str] = None
    expires_at_ms: Optional[int] = None
    last_refresh: Optional[str] = None
    token_type: Optional[str] = None
    scope: Optional[str] = None
    client_id: Optional[str] = None
    portal_base_url: Optional[str] = None
    inference_base_url: Optional[str] = None
    obtained_at: Optional[str] = None
    expires_in: Optional[int] = None
    agent_key: Optional[str] = None
    agent_key_id: Optional[str] = None
    agent_key_expires_at: Optional[str] = None
    agent_key_expires_in: Optional[int] = None
    agent_key_reused: Optional[bool] = None
    agent_key_obtained_at: Optional[str] = None
    tls: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, provider: str, payload: Dict[str, Any]) -> "PooledCredential":
        allowed = {f.name for f in fields(cls) if f.name != "provider"}
        data = {k: payload.get(k) for k in allowed if k in payload}
        data.setdefault("id", uuid.uuid4().hex[:6])
        data.setdefault("label", payload.get("source", provider))
        data.setdefault("auth_type", "api_key")
        data.setdefault("priority", 0)
        data.setdefault("source", "manual")
        data.setdefault("access_token", "")
        return cls(provider=provider, **data)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for field_def in fields(self):
            if field_def.name == "provider":
                continue
            value = getattr(self, field_def.name)
            if value is not None:
                result[field_def.name] = value
        for key in ("last_status", "last_status_at", "last_error_code"):
            result.setdefault(key, getattr(self, key))
        return result

    @property
    def runtime_api_key(self) -> str:
        if self.provider == "nous":
            return str(self.agent_key or self.access_token or "")
        return str(self.access_token or "")

    @property
    def runtime_base_url(self) -> Optional[str]:
        if self.provider == "nous":
            return self.inference_base_url or self.base_url
        return self.base_url


def _label_from_token(token: str, fallback: str) -> str:
    claims = _decode_jwt_claims(token)
    for key in ("email", "preferred_username", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _next_priority(entries: List[PooledCredential]) -> int:
    return max((entry.priority for entry in entries), default=-1) + 1


def _is_manual_source(source: str) -> bool:
    normalized = (source or "").strip().lower()
    return normalized == "manual" or normalized.startswith("manual:")


class CredentialPool:
    def __init__(self, provider: str, entries: List[PooledCredential]):
        self.provider = provider
        self._entries = sorted(entries, key=lambda entry: entry.priority)
        self._current_id: Optional[str] = None

    def has_credentials(self) -> bool:
        return bool(self._entries)

    def entries(self) -> List[PooledCredential]:
        return list(sorted(self._entries, key=lambda entry: entry.priority))

    def current(self) -> Optional[PooledCredential]:
        if not self._current_id:
            return None
        return next((entry for entry in self._entries if entry.id == self._current_id), None)

    def _persist(self) -> None:
        write_credential_pool(
            self.provider,
            [entry.to_dict() for entry in sorted(self._entries, key=lambda item: item.priority)],
        )

    def _mark_exhausted(self, entry: PooledCredential, status_code: Optional[int]) -> None:
        entry.last_status = "exhausted"
        entry.last_status_at = time.time()
        entry.last_error_code = status_code
        self._persist()

    def _refresh_entry(self, entry: PooledCredential, *, force: bool) -> Optional[PooledCredential]:
        if entry.auth_type != "oauth" or not entry.refresh_token:
            if force:
                self._mark_exhausted(entry, None)
            return None

        try:
            if self.provider == "anthropic":
                from agent.anthropic_adapter import refresh_anthropic_oauth_pure

                refreshed = refresh_anthropic_oauth_pure(
                    entry.refresh_token,
                    use_json=entry.source.endswith("hermes_pkce"),
                )
                entry.access_token = refreshed["access_token"]
                entry.refresh_token = refreshed["refresh_token"]
                entry.expires_at_ms = refreshed["expires_at_ms"]
            elif self.provider == "openai-codex":
                refreshed = auth_mod.refresh_codex_oauth_pure(
                    entry.access_token,
                    entry.refresh_token,
                )
                entry.access_token = refreshed["access_token"]
                entry.refresh_token = refreshed["refresh_token"]
                entry.last_refresh = refreshed.get("last_refresh")
            elif self.provider == "nous":
                refreshed = auth_mod.refresh_nous_oauth_pure(
                    entry.access_token,
                    entry.refresh_token,
                    entry.client_id or "hermes-cli",
                    entry.portal_base_url or "https://portal.nousresearch.com",
                    entry.inference_base_url or "https://inference-api.nousresearch.com/v1",
                    token_type=entry.token_type or "Bearer",
                    scope=entry.scope or "",
                    obtained_at=entry.obtained_at,
                    expires_at=entry.expires_at,
                    agent_key=entry.agent_key,
                    agent_key_expires_at=entry.agent_key_expires_at,
                    min_key_ttl_seconds=DEFAULT_AGENT_KEY_MIN_TTL_SECONDS,
                    force_refresh=force,
                    force_mint=force,
                )
                for key, value in refreshed.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
            else:
                return entry
        except Exception:
            self._mark_exhausted(entry, None)
            return None

        entry.last_status = "ok"
        entry.last_status_at = None
        entry.last_error_code = None
        self._persist()
        return entry

    def _entry_needs_refresh(self, entry: PooledCredential) -> bool:
        if entry.auth_type != "oauth":
            return False
        if self.provider == "anthropic":
            if entry.expires_at_ms is None:
                return False
            return int(entry.expires_at_ms) <= int(time.time() * 1000) + 120_000
        if self.provider == "openai-codex":
            return _codex_access_token_is_expiring(
                entry.access_token,
                CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
            )
        if self.provider == "nous":
            if _is_expiring(entry.expires_at, ACCESS_TOKEN_REFRESH_SKEW_SECONDS):
                return True
            return not _agent_key_is_usable(
                {
                    "agent_key": entry.agent_key,
                    "agent_key_expires_at": entry.agent_key_expires_at,
                },
                DEFAULT_AGENT_KEY_MIN_TTL_SECONDS,
            )
        return False

    def select(self) -> Optional[PooledCredential]:
        now = time.time()
        for entry in sorted(self._entries, key=lambda item: item.priority):
            if entry.last_status == "exhausted":
                if entry.last_status_at and now - entry.last_status_at < EXHAUSTED_TTL_SECONDS:
                    continue
                entry.last_status = "ok"
                entry.last_status_at = None
                entry.last_error_code = None
                self._persist()
            if self._entry_needs_refresh(entry):
                refreshed = self._refresh_entry(entry, force=False)
                if refreshed is None:
                    continue
                entry = refreshed
            self._current_id = entry.id
            return entry
        self._current_id = None
        return None

    def mark_exhausted_and_rotate(self, *, status_code: Optional[int]) -> Optional[PooledCredential]:
        entry = self.current() or self.select()
        if entry is None:
            return None
        self._mark_exhausted(entry, status_code)
        self._current_id = None
        return self.select()

    def try_refresh_current(self) -> Optional[PooledCredential]:
        entry = self.current()
        if entry is None:
            return None
        refreshed = self._refresh_entry(entry, force=True)
        if refreshed is not None:
            self._current_id = refreshed.id
        return refreshed

    def reset_statuses(self) -> int:
        count = 0
        for entry in self._entries:
            if entry.last_status or entry.last_status_at or entry.last_error_code:
                entry.last_status = None
                entry.last_status_at = None
                entry.last_error_code = None
                count += 1
        if count:
            self._persist()
        return count

    def remove_index(self, index: int) -> Optional[PooledCredential]:
        ordered = sorted(self._entries, key=lambda item: item.priority)
        if index < 1 or index > len(ordered):
            return None
        removed = ordered.pop(index - 1)
        for new_priority, entry in enumerate(ordered):
            entry.priority = new_priority
        self._entries = ordered
        self._persist()
        if self._current_id == removed.id:
            self._current_id = None
        return removed

    def add_entry(self, entry: PooledCredential) -> PooledCredential:
        entry.priority = _next_priority(self._entries)
        self._entries.append(entry)
        self._persist()
        return entry


def _upsert_entry(entries: List[PooledCredential], provider: str, source: str, payload: Dict[str, Any]) -> bool:
    existing = next((entry for entry in entries if entry.source == source), None)
    if existing is None:
        payload.setdefault("id", uuid.uuid4().hex[:6])
        payload.setdefault("priority", _next_priority(entries))
        payload.setdefault("label", payload.get("label") or source)
        entries.append(PooledCredential.from_dict(provider, payload))
        return True

    changed = False
    for key, value in payload.items():
        if key in {"id", "priority"} or value is None:
            continue
        if key == "label" and existing.label:
            continue
        if hasattr(existing, key) and getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True
    return changed


def _seed_from_env(provider: str, entries: List[PooledCredential]) -> bool:
    changed = False
    if provider == "openrouter":
        token = os.getenv("OPENROUTER_API_KEY", "").strip()
        if token:
            changed |= _upsert_entry(
                entries,
                provider,
                "env:OPENROUTER_API_KEY",
                {
                    "source": "env:OPENROUTER_API_KEY",
                    "auth_type": "api_key",
                    "access_token": token,
                    "base_url": OPENROUTER_BASE_URL,
                    "label": "OPENROUTER_API_KEY",
                },
            )
        return changed

    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig or pconfig.auth_type != "api_key":
        return changed

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip().rstrip("/")

    env_vars = list(pconfig.api_key_env_vars)
    if provider == "anthropic":
        env_vars = [
            "ANTHROPIC_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
        ]

    for env_var in env_vars:
        token = os.getenv(env_var, "").strip()
        if not token:
            continue
        auth_type = "oauth" if provider == "anthropic" and not token.startswith("sk-ant-api") else "api_key"
        base_url = env_url or pconfig.inference_base_url
        changed |= _upsert_entry(
            entries,
            provider,
            f"env:{env_var}",
            {
                "source": f"env:{env_var}",
                "auth_type": auth_type,
                "access_token": token,
                "base_url": base_url,
                "label": env_var,
            },
        )
    return changed


def _normalize_pool_priorities(provider: str, entries: List[PooledCredential]) -> bool:
    if provider != "anthropic":
        return False

    source_rank = {
        "env:ANTHROPIC_TOKEN": 0,
        "env:CLAUDE_CODE_OAUTH_TOKEN": 1,
        "hermes_pkce": 2,
        "claude_code": 3,
        "env:ANTHROPIC_API_KEY": 4,
    }
    manual_entries = sorted(
        (entry for entry in entries if _is_manual_source(entry.source)),
        key=lambda entry: entry.priority,
    )
    seeded_entries = sorted(
        (entry for entry in entries if not _is_manual_source(entry.source)),
        key=lambda entry: (
            source_rank.get(entry.source, len(source_rank)),
            entry.priority,
            entry.label,
        ),
    )

    changed = False
    for new_priority, entry in enumerate([*manual_entries, *seeded_entries]):
        if entry.priority != new_priority:
            entry.priority = new_priority
            changed = True
    return changed


def _seed_from_singletons(provider: str, entries: List[PooledCredential]) -> bool:
    changed = False
    auth_store = _load_auth_store()

    if provider == "anthropic":
        from agent.anthropic_adapter import read_claude_code_credentials, read_hermes_oauth_credentials

        hermes_creds = read_hermes_oauth_credentials()
        if hermes_creds and hermes_creds.get("accessToken"):
            changed |= _upsert_entry(
                entries,
                provider,
                "hermes_pkce",
                {
                    "source": "hermes_pkce",
                    "auth_type": "oauth",
                    "access_token": hermes_creds.get("accessToken", ""),
                    "refresh_token": hermes_creds.get("refreshToken"),
                    "expires_at_ms": hermes_creds.get("expiresAt"),
                    "label": _label_from_token(hermes_creds.get("accessToken", ""), "hermes_pkce"),
                },
            )
        claude_creds = read_claude_code_credentials()
        if claude_creds and claude_creds.get("accessToken"):
            changed |= _upsert_entry(
                entries,
                provider,
                "claude_code",
                {
                    "source": "claude_code",
                    "auth_type": "oauth",
                    "access_token": claude_creds.get("accessToken", ""),
                    "refresh_token": claude_creds.get("refreshToken"),
                    "expires_at_ms": claude_creds.get("expiresAt"),
                    "label": _label_from_token(claude_creds.get("accessToken", ""), "claude_code"),
                },
            )

    elif provider == "nous":
        state = _load_provider_state(auth_store, "nous")
        if state:
            changed |= _upsert_entry(
                entries,
                provider,
                "device_code",
                {
                    "source": "device_code",
                    "auth_type": "oauth",
                    "access_token": state.get("access_token", ""),
                    "refresh_token": state.get("refresh_token"),
                    "expires_at": state.get("expires_at"),
                    "token_type": state.get("token_type"),
                    "scope": state.get("scope"),
                    "client_id": state.get("client_id"),
                    "portal_base_url": state.get("portal_base_url"),
                    "inference_base_url": state.get("inference_base_url"),
                    "agent_key": state.get("agent_key"),
                    "agent_key_expires_at": state.get("agent_key_expires_at"),
                    "label": _label_from_token(state.get("access_token", ""), "device_code"),
                },
            )

    elif provider == "openai-codex":
        state = _load_provider_state(auth_store, "openai-codex")
        tokens = state.get("tokens") if isinstance(state, dict) else None
        if isinstance(tokens, dict) and tokens.get("access_token"):
            changed |= _upsert_entry(
                entries,
                provider,
                "device_code",
                {
                    "source": "device_code",
                    "auth_type": "oauth",
                    "access_token": tokens.get("access_token", ""),
                    "refresh_token": tokens.get("refresh_token"),
                    "base_url": "https://chatgpt.com/backend-api/codex",
                    "last_refresh": state.get("last_refresh"),
                    "label": _label_from_token(tokens.get("access_token", ""), "device_code"),
                },
            )

    return changed


def load_pool(provider: str) -> CredentialPool:
    provider = (provider or "").strip().lower()
    raw_entries = read_credential_pool(provider)
    entries = [PooledCredential.from_dict(provider, payload) for payload in raw_entries]
    changed = _seed_from_singletons(provider, entries)
    changed |= _seed_from_env(provider, entries)
    changed |= _normalize_pool_priorities(provider, entries)
    if changed:
        write_credential_pool(
            provider,
            [entry.to_dict() for entry in sorted(entries, key=lambda item: item.priority)],
        )
    return CredentialPool(provider, entries)
