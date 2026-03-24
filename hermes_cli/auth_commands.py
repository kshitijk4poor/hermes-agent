"""Credential-pool auth subcommands."""

from __future__ import annotations

from getpass import getpass
import uuid

from agent.credential_pool import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH,
    SOURCE_MANUAL,
    STATUS_EXHAUSTED,
    PooledCredential,
    _label_from_token,
    load_pool,
)
import hermes_cli.auth as auth_mod
from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_constants import OPENROUTER_BASE_URL


def _normalize_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized in {"or", "open-router"}:
        return "openrouter"
    return normalized


def _provider_base_url(provider: str) -> str:
    if provider == "openrouter":
        return OPENROUTER_BASE_URL
    pconfig = PROVIDER_REGISTRY.get(provider)
    return pconfig.inference_base_url if pconfig else ""


def _oauth_default_label(provider: str, count: int) -> str:
    return f"{provider}-oauth-{count}"


def _api_key_default_label(count: int) -> str:
    return f"api-key-{count}"


def _display_source(source: str) -> str:
    return source.split(":", 1)[1] if source.startswith("manual:") else source


def auth_add_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", ""))
    if provider not in PROVIDER_REGISTRY and provider != "openrouter":
        raise SystemExit(f"Unknown provider: {provider}")

    requested_type = str(getattr(args, "auth_type", "") or "").strip().lower()
    if requested_type in {AUTH_TYPE_API_KEY, "api-key"}:
        requested_type = AUTH_TYPE_API_KEY
    if not requested_type:
        requested_type = AUTH_TYPE_OAUTH if provider in {"anthropic", "nous", "openai-codex"} else AUTH_TYPE_API_KEY

    pool = load_pool(provider)

    if requested_type == AUTH_TYPE_API_KEY:
        token = (getattr(args, "api_key", None) or "").strip()
        if not token:
            token = getpass("Paste your API key: ").strip()
        if not token:
            raise SystemExit("No API key provided.")
        default_label = _api_key_default_label(len(pool.entries()) + 1)
        label = (getattr(args, "label", None) or "").strip()
        if not label:
            label = input(f"Label (optional, default: {default_label}): ").strip() or default_label
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=SOURCE_MANUAL,
            access_token=token,
            base_url=_provider_base_url(provider),
        )
        pool.add_entry(entry)
        print(f'Added {provider} credential #{len(pool.entries())}: "{label}"')
        return

    if provider == "anthropic":
        from agent import anthropic_adapter as anthropic_mod

        creds = anthropic_mod.run_hermes_oauth_login_pure()
        if not creds:
            raise SystemExit("Anthropic OAuth login did not return credentials.")
        label = (getattr(args, "label", None) or "").strip() or _label_from_token(
            creds["access_token"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:hermes_pkce",
            access_token=creds["access_token"],
            refresh_token=creds.get("refresh_token"),
            expires_at_ms=creds.get("expires_at_ms"),
            base_url=_provider_base_url(provider),
        )
        pool.add_entry(entry)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    if provider == "nous":
        creds = auth_mod._nous_device_code_login(
            portal_base_url=getattr(args, "portal_url", None),
            inference_base_url=getattr(args, "inference_url", None),
            client_id=getattr(args, "client_id", None),
            scope=getattr(args, "scope", None),
            open_browser=not getattr(args, "no_browser", False),
            timeout_seconds=getattr(args, "timeout", None) or 15.0,
            insecure=bool(getattr(args, "insecure", False)),
            ca_bundle=getattr(args, "ca_bundle", None),
            min_key_ttl_seconds=max(60, int(getattr(args, "min_key_ttl_seconds", 5 * 60))),
        )
        label = (getattr(args, "label", None) or "").strip() or _label_from_token(
            creds.get("access_token", ""),
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        entry = PooledCredential.from_dict(provider, {
            **creds,
            "label": label,
            "auth_type": AUTH_TYPE_OAUTH,
            "source": f"{SOURCE_MANUAL}:device_code",
            "base_url": creds.get("inference_base_url"),
        })
        pool.add_entry(entry)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    if provider == "openai-codex":
        creds = auth_mod._codex_device_code_login()
        label = (getattr(args, "label", None) or "").strip() or _label_from_token(
            creds["tokens"]["access_token"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:device_code",
            access_token=creds["tokens"]["access_token"],
            refresh_token=creds["tokens"].get("refresh_token"),
            base_url=creds.get("base_url"),
            last_refresh=creds.get("last_refresh"),
        )
        pool.add_entry(entry)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    raise SystemExit(f"`hermes auth add {provider}` is not implemented for auth type {requested_type} yet.")


def auth_list_command(args) -> None:
    provider_filter = _normalize_provider(getattr(args, "provider", "") or "")
    providers = [provider_filter] if provider_filter else sorted({
        *PROVIDER_REGISTRY.keys(),
        "openrouter",
    })
    for provider in providers:
        pool = load_pool(provider)
        entries = pool.entries()
        if not entries:
            continue
        current = pool.peek()
        print(f"{provider} ({len(entries)} credentials):")
        for idx, entry in enumerate(entries, start=1):
            marker = "  "
            if current is not None and entry.id == current.id:
                marker = "← "
            status = ""
            if entry.last_status == STATUS_EXHAUSTED:
                status = f" exhausted ({entry.last_error_code})"
            source = _display_source(entry.source)
            print(f"  #{idx}  {entry.label:<20} {entry.auth_type:<7} {source}{status} {marker}".rstrip())
        print()


def auth_remove_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", ""))
    index = int(getattr(args, "index"))
    pool = load_pool(provider)
    removed = pool.remove_index(index)
    if removed is None:
        raise SystemExit(f"No credential #{index} for provider {provider}.")
    print(f"Removed {provider} credential #{index} ({removed.label})")


def auth_reset_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", ""))
    pool = load_pool(provider)
    count = pool.reset_statuses()
    print(f"Reset status on {count} {provider} credentials")


def auth_command(args) -> None:
    action = getattr(args, "auth_action", "")
    if action == "add":
        auth_add_command(args)
        return
    if action == "list":
        auth_list_command(args)
        return
    if action == "remove":
        auth_remove_command(args)
        return
    if action == "reset":
        auth_reset_command(args)
        return
    raise SystemExit("Usage: hermes auth [add|list|remove|reset] ...")
