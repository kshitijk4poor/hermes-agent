# Multi-Credential OAuth Fallback

**Date:** 2026-03-23
**Status:** Design v3 — implementation-ready

## Problem

Hermes supports one credential per provider. When it runs out of credits (402) or hits hard rate limits (429), the user is stuck. Users with multiple OAuth accounts (e.g., personal Claude Pro + work Claude Max + API key) can't leverage them.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Registration UX | `hermes auth add <provider>` for both OAuth and API keys | Pool is the single authority — all credential types managed through one CLI |
| Rotation trigger | Rotate on 402 immediately; retry-then-rotate on 429 | Distinguishes transient throttle from hard credit cap |
| All exhausted | Fall through to existing cross-provider `_try_activate_fallback()` | Credential rotation = inner loop; cross-provider = outer loop |
| State persistence | Persist `last_status` + `last_status_at` to `auth.json`, 24h TTL | Avoids re-probing dead creds; TTL prevents stale-state bugs |
| Selection strategy | Fill-first (exhaust primary before advancing) | Matches "use primary until exhausted" goal |
| Pool entries | Provider-specific types, not generic + opaque bag | Each provider's refresh needs different state; typed entries make schema self-documenting |
| API key authority | Pool owns all keys — env vars seed pool on first run | One source of truth, no ambiguity between env/config.yaml/pool |
| Startup credential | `runtime_provider.py` consults pool | Pool is authoritative for initial credential, not env-var chain |
| Auxiliary clients | Independent — read pool `last_status` to skip dead creds only | Low-volume tasks; full pool wiring is disproportionate for v1 |

---

## Data Model

### Provider-Specific Pool Entries

Stored in `~/.hermes/auth.json` under `credential_pool`. Each provider defines its own entry schema carrying exactly the fields its refresh logic needs.

#### Anthropic

```json
{
  "credential_pool": {
    "anthropic": [
      {
        "id": "a1b2c3",
        "label": "user@gmail.com",
        "auth_type": "oauth",
        "priority": 0,
        "source": "claude_code",
        "access_token": "sk-ant-oat-...",
        "refresh_token": "rt-...",
        "expires_at_ms": 1711234567000,
        "last_status": "ok",
        "last_status_at": null,
        "last_error_code": null
      },
      {
        "id": "d4e5f6",
        "label": "work@company.com",
        "auth_type": "oauth",
        "priority": 1,
        "source": "hermes_pkce",
        "access_token": "sk-ant-oat-...",
        "refresh_token": "rt-...",
        "expires_at_ms": 1711234999000,
        "last_status": "exhausted",
        "last_status_at": 1711230000.0,
        "last_error_code": 402
      },
      {
        "id": "g7h8i9",
        "label": "work-budget",
        "auth_type": "api_key",
        "priority": 2,
        "source": "manual",
        "access_token": "sk-ant-api-...",
        "refresh_token": null,
        "expires_at_ms": null,
        "last_status": "ok",
        "last_status_at": null,
        "last_error_code": null
      }
    ]
  }
}
```

Refresh needs: `refresh_token` only. The Anthropic OAuth token exchange returns a new `access_token` + `refresh_token` + `expires_in`. No extra state.

#### Nous

```json
{
  "credential_pool": {
    "nous": [
      {
        "id": "n1o2u3",
        "label": "user@nous.com",
        "auth_type": "oauth",
        "priority": 0,
        "source": "device_code",
        "access_token": "eyJ...",
        "refresh_token": "rt-...",
        "expires_at": "2026-03-24T12:00:00+00:00",
        "token_type": "Bearer",
        "scope": "inference:mint_agent_key",
        "client_id": "hermes-cli",
        "portal_base_url": "https://portal.nousresearch.com",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
        "agent_key": "ak-...",
        "agent_key_expires_at": "2026-03-23T13:30:00+00:00",
        "last_status": "ok",
        "last_status_at": null,
        "last_error_code": null
      }
    ]
  }
}
```

Refresh needs: `access_token`, `refresh_token`, `client_id`, `portal_base_url` for token refresh; then `access_token`, `portal_base_url`, `inference_base_url` for agent key minting. This is the full state currently in `auth.json → providers.nous`.

#### Codex

```json
{
  "credential_pool": {
    "openai-codex": [
      {
        "id": "c1d2x3",
        "label": "user@openai.com",
        "auth_type": "oauth",
        "priority": 0,
        "source": "device_code",
        "access_token": "eyJ...",
        "refresh_token": "rt-...",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "last_refresh": "2026-03-23T10:00:00Z",
        "last_status": "ok",
        "last_status_at": null,
        "last_error_code": null
      }
    ]
  }
}
```

Refresh needs: `access_token`, `refresh_token`. Returns new tokens dict. `base_url` is carried per-entry because it can vary.

#### API-Key Providers (generic)

For providers that only use API keys (OpenRouter, Z.AI, Kimi, MiniMax, DeepSeek, etc.), entries are simpler:

```json
{
  "credential_pool": {
    "openrouter": [
      {
        "id": "or1234",
        "label": "personal",
        "auth_type": "api_key",
        "priority": 0,
        "source": "env:OPENROUTER_API_KEY",
        "access_token": "sk-or-...",
        "refresh_token": null,
        "base_url": "https://openrouter.ai/api/v1",
        "last_status": "ok",
        "last_status_at": null,
        "last_error_code": null
      }
    ]
  }
}
```

No refresh logic — API keys are static. Rotation still works on 402/429.

### Common Fields (all entry types)

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | Unique ID (hex, assigned at registration) |
| `label` | str | Display name (auto-extracted JWT email or user-provided) |
| `auth_type` | str | `"oauth"` or `"api_key"` |
| `priority` | int | Lower = tried first (fill-first). Set at registration time. |
| `source` | str | Provenance: `claude_code`, `hermes_pkce`, `device_code`, `env:VAR_NAME`, `manual` |
| `access_token` | str | The token used for API calls |
| `refresh_token` | str? | OAuth refresh token (null for API keys) |
| `last_status` | str? | `"ok"`, `"exhausted"`, or null |
| `last_status_at` | float? | Unix timestamp of last status change |
| `last_error_code` | int? | HTTP status code that caused exhaustion |

---

## Single-Authority API Key Storage

### The Problem Today

API keys currently come from three sources with no single owner:
1. **Env vars** — `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, etc. (resolved by `runtime_provider.py`)
2. **Config.yaml** — `model.api_key` for custom endpoints
3. **Future: `hermes auth add --type api-key`** — manual pool registration

### The Solution: Pool Owns Everything

The pool is the single source of truth for all credentials. Env vars and config.yaml become **seed sources** that populate the pool, not runtime resolution paths.

**Seeding rules (on pool load):**

1. For each provider in `PROVIDER_REGISTRY` with `api_key_env_vars`:
   - Check each env var in priority order
   - If set and no pool entry exists with `source: "env:VAR_NAME"` → create one at lowest priority
   - If set and a pool entry with that source already exists → update `access_token` if changed (env var wins on conflict — user may have rotated the key)
2. For Anthropic specifically, also check:
   - `~/.claude/.credentials.json` → seed as `source: "claude_code"` OAuth entry
   - `~/.hermes/.anthropic_oauth.json` → seed as `source: "hermes_pkce"` OAuth entry
3. For Nous/Codex, also check:
   - `auth.json → providers.nous` → seed as `source: "device_code"` OAuth entry
   - `auth.json → providers.openai-codex` → seed as `source: "device_code"` OAuth entry

**Key property:** seeding is additive and idempotent. Existing pool entries are never deleted by seeding. Manual entries (`source: "manual"`) are never touched.

**After seeding, runtime_provider.py calls `pool.select()` instead of its own env-var chain.** The pool returns the first non-exhausted credential by priority.

### What Happens to Env Vars

Env vars still work — they seed the pool transparently. A user who sets `ANTHROPIC_API_KEY` and never runs `hermes auth add` gets exactly the same behavior as today: one credential, no rotation. The pool is invisible until they add a second credential.

If a user later runs `hermes auth add anthropic --type api-key`, the new key gets priority after the env-var-seeded entry. They now have rotation.

---

## Refresh Architecture

### Pure Refresh Functions (New)

Each OAuth provider gets a pure function that takes credential state in and returns updated state out, with **no file writes**:

```python
# agent/anthropic_adapter.py
def refresh_anthropic_oauth_pure(refresh_token: str) -> Dict[str, Any]:
    """Token exchange only. No file writes.
    Returns: {"access_token": str, "refresh_token": str, "expires_at_ms": int}
    """
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        "https://console.anthropic.com/v1/oauth/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"claude-cli/{_CLAUDE_CODE_VERSION} (external, cli)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
    return {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token", refresh_token),
        "expires_at_ms": int(time.time() * 1000) + (result.get("expires_in", 3600) * 1000),
    }
```

```python
# hermes_cli/auth.py
def refresh_nous_oauth_pure(
    access_token: str,
    refresh_token: str,
    client_id: str,
    portal_base_url: str,
    inference_base_url: str,
    *,
    min_key_ttl_seconds: int = 1800,
    timeout_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Refresh Nous access token + mint agent key. No auth.json writes.
    Returns updated state dict with all Nous-specific fields.
    """
    # Step 1: refresh access_token if expiring (same HTTP call as _refresh_access_token)
    # Step 2: mint agent key (same HTTP call as _mint_agent_key)
    # Returns: {"access_token", "refresh_token", "expires_at", "agent_key",
    #           "agent_key_expires_at", "inference_base_url", ...}
    ...

def refresh_codex_oauth_pure(
    access_token: str,
    refresh_token: str,
    *,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """Refresh Codex OAuth tokens. No auth.json writes.
    Returns: {"access_token": str, "refresh_token": str}
    """
    # Same HTTP call as _refresh_codex_auth_tokens
    ...
```

### Existing Functions Refactored (Backward Compat)

The existing singleton functions call the new pure functions + write to their singleton files. No behavior change for code that doesn't use the pool.

```python
# BEFORE:
def _refresh_oauth_token(creds):
    # ... HTTP call ...
    _write_claude_code_credentials(new_access, new_refresh, new_expires_ms)
    return new_access

# AFTER:
def _refresh_oauth_token(creds):
    result = refresh_anthropic_oauth_pure(creds["refreshToken"])
    _write_claude_code_credentials(
        result["access_token"], result["refresh_token"], result["expires_at_ms"]
    )
    return result["access_token"]
```

### Pool Refresh Flow

```
pool.try_refresh(entry) → updated_entry | None:
  1. Dispatch to provider-specific pure refresh:
     - anthropic → refresh_anthropic_oauth_pure(entry.refresh_token)
     - nous → refresh_nous_oauth_pure(entry.access_token, entry.refresh_token, ...)
     - codex → refresh_codex_oauth_pure(entry.access_token, entry.refresh_token)
  2. On success:
     - Update entry fields in-memory (access_token, refresh_token, expires_at, etc.)
     - Persist updated pool entry to auth.json (pool's section, not singleton files)
     - Return updated entry
  3. On failure:
     - Mark entry exhausted (last_status="exhausted", last_status_at=now)
     - Persist status to auth.json
     - Return None
```

**Key guarantee:** refreshing entry B never touches entry A. Each entry carries its own state, and the pure refresh functions have no side effects.

---

## Startup Wiring

### runtime_provider.py Changes

`resolve_runtime_provider()` currently resolves credentials via provider-specific chains (env vars, auth.json singletons, file reads). After this change:

```python
def resolve_runtime_provider(*, requested=None, explicit_api_key=None, explicit_base_url=None):
    # ... existing provider resolution (which provider to use) stays the same ...

    provider = resolve_provider(requested, ...)

    # NEW: consult pool for initial credential
    from agent.credential_pool import load_pool
    pool = load_pool(provider)

    if pool and pool.has_credentials():
        entry = pool.select()
        if entry:
            return {
                "provider": provider,
                "api_mode": _api_mode_for_provider(provider, entry),
                "base_url": _base_url_for_entry(provider, entry),
                "api_key": entry.access_token,
                "source": entry.source,
                "credential_pool": pool,  # pass pool to AIAgent for rotation
                # ... provider-specific fields from entry ...
            }

    # FALLBACK: no pool or pool empty — use existing resolution
    # (this path handles first-time users who haven't run setup yet)
    if provider == "nous":
        creds = resolve_nous_runtime_credentials(...)
        ...
    elif provider == "anthropic":
        ...
```

The pool is passed to `AIAgent` via the runtime dict so the agent can rotate credentials mid-conversation without re-resolving.

### AIAgent.__init__ Changes

```python
class AIAgent:
    def __init__(self, ..., credential_pool=None):
        self._credential_pool = credential_pool
        # ... existing init ...
```

### Gateway Startup

Gateway creates `AIAgent` instances per session. Since `resolve_runtime_provider()` now returns the pool, gateway gets rotation for free:

```python
# gateway/run.py — existing code already calls resolve_runtime_provider()
runtime = resolve_runtime_provider(requested=config.get("provider"))
agent = AIAgent(..., credential_pool=runtime.get("credential_pool"))
```

No additional gateway changes needed.

---

## Runtime Flow

### Credential Selection (fill-first)

```
pool.select():
  1. For each entry by priority (ascending):
     a. If last_status == "exhausted" and now - last_status_at < 86400 → skip
     b. If last_status == "exhausted" and now - last_status_at >= 86400 → reset to "ok"
     c. If auth_type == "oauth" and token expires within 120s:
        - try_refresh(entry)
        - If refresh fails → mark exhausted, continue to next
     d. Return this entry
  2. All skipped/exhausted → return None
```

### Error Handling in run_agent.py

Replaces the three provider-specific `if/elif` blocks (~lines 6104-6147):

```python
# In the except block, after status_code is extracted:

# Credential pool rotation (replaces 3 provider-specific refresh blocks)
if self._credential_pool:
    if status_code == 402:
        prev = self._credential_pool.current()
        next_entry = self._credential_pool.mark_exhausted_and_rotate(
            status_code=402)
        if next_entry:
            self._swap_credential(next_entry)
            print(f"{self.log_prefix}🔐 {prev.label} exhausted (402), "
                  f"switching to {next_entry.label}")
            retry_count = 0
            continue
        # All exhausted — fall through to cross-provider fallback below

    elif status_code == 429 and retry_429_with_same_cred:
        # Second 429 on same credential for this request
        prev = self._credential_pool.current()
        next_entry = self._credential_pool.mark_exhausted_and_rotate(
            status_code=429)
        if next_entry:
            self._swap_credential(next_entry)
            print(f"{self.log_prefix}🔐 {prev.label} rate-limited (429), "
                  f"switching to {next_entry.label}")
            retry_count = 0
            continue

    elif status_code == 429 and not retry_429_with_same_cred:
        retry_429_with_same_cred = True
        # Fall through to existing backoff logic (retry same credential)

    elif status_code == 401:
        refreshed = self._credential_pool.try_refresh_current()
        if refreshed:
            self._swap_credential(refreshed)
            print(f"{self.log_prefix}🔐 Credentials refreshed, retrying...")
            continue
        # Refresh failed — show existing diagnostic output
```

### _swap_credential (replaces 3 methods)

```python
def _swap_credential(self, entry):
    """Hot-swap the active credential. Dispatches by api_mode."""
    if self.api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client, _is_oauth_token
        try:
            self._anthropic_client.close()
        except Exception:
            pass
        self._anthropic_api_key = entry.access_token
        self._anthropic_client = build_anthropic_client(
            entry.access_token, self._anthropic_base_url)
        self._is_anthropic_oauth = _is_oauth_token(entry.access_token)

    elif self.api_mode == "codex_responses":
        self.api_key = entry.access_token
        self.base_url = getattr(entry, "base_url", self.base_url)
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        self._replace_primary_openai_client(reason="credential_rotation")

    elif self.api_mode == "chat_completions":
        self.api_key = entry.access_token
        base = getattr(entry, "inference_base_url", None) or self.base_url
        self.base_url = base
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        self._client_kwargs.pop("default_headers", None)
        self._replace_primary_openai_client(reason="credential_rotation")
```

### Deleted Methods

- `_try_refresh_codex_client_credentials()` (~30 lines)
- `_try_refresh_nous_client_credentials()` (~35 lines)
- `_try_refresh_anthropic_client_credentials()` (~40 lines)

Total: ~105 lines removed, replaced by `_swap_credential()` (~30 lines) + pool delegation.

---

## CLI Commands

### `hermes auth add <provider>`

```
$ hermes auth add anthropic

  How would you like to authenticate?
  1. Claude Pro/Max subscription (OAuth login)
  2. API key

  > 1
  Running OAuth flow...
  [browser opens, user logs in with different account]

  ✓ Authenticated as work@company.com
  ✓ Added as anthropic credential #2 (priority 1)
```

```
$ hermes auth add anthropic --type api-key
  Paste your API key: sk-ant-api-***
  Label (optional, default: api-key-1): work-budget

  ✓ Added as anthropic credential #3: "work-budget" (priority 2)
```

Implementation: reuses existing OAuth flows from `setup.py` (`run_oauth_setup_token()`, device code flow, etc.) — the pool just stores the result instead of the singleton file.

### `hermes auth list`

```
$ hermes auth list

  anthropic (3 credentials):
    #1  user@gmail.com     oauth   claude_code       ← active
    #2  work@company.com   oauth   hermes_pkce       exhausted (402, 2h ago)
    #3  work-budget        api_key manual

  nous (1 credential):
    #1  user@nous.com      oauth   device_code       ← active

  openrouter (1 credential):
    #1  personal           api_key env:OPENROUTER_API_KEY  ← active
```

### `hermes auth remove <provider> <index>`

```
$ hermes auth remove anthropic 2
  ✓ Removed anthropic credential #2 (work@company.com)
  Remaining credentials re-prioritized.
```

### `hermes auth reset <provider>`

Clears `last_status` on all credentials for a provider (manual recovery):

```
$ hermes auth reset anthropic
  ✓ Reset status on 3 anthropic credentials
```

---

## Backward Compatibility

### Auto-Migration (First Pool Load)

When `credential_pool` is absent in `auth.json`, `load_pool()` runs migration:

1. **Anthropic:** Walk the existing `resolve_anthropic_token()` priority chain. For each source that has a credential, create a pool entry:
   - `ANTHROPIC_TOKEN` env → entry with `source: "env:ANTHROPIC_TOKEN"`
   - `~/.hermes/.anthropic_oauth.json` → entry with `source: "hermes_pkce"`
   - `~/.claude/.credentials.json` → entry with `source: "claude_code"`
   - `ANTHROPIC_API_KEY` env → entry with `source: "env:ANTHROPIC_API_KEY"`
   - Priority follows the existing resolution order (first found = priority 0)

2. **Nous:** Copy `auth.json → providers.nous` state into a pool entry with `source: "device_code"`.

3. **Codex:** Copy `auth.json → providers.openai-codex` state into a pool entry with `source: "device_code"`.

4. **API-key providers:** For each provider in `PROVIDER_REGISTRY` with `api_key_env_vars`, check env vars and create entries.

**Migration is additive.** Original singleton state is preserved (existing code paths still work). The pool is written alongside, and `runtime_provider.py` prefers it when present.

### Env Var Re-Seeding (Every Pool Load)

On every `load_pool()`, env vars are re-checked:
- If env var value changed since last seed → update the pool entry's `access_token`
- If env var is newly set → create entry at lowest priority
- If env var is now empty but pool entry with that source exists → keep pool entry (user may have moved the key to pool-only)

This ensures `export ANTHROPIC_API_KEY=new-key` takes effect without `hermes auth add`.

---

## Auxiliary Clients

`auxiliary_client.py` does **not** use the pool for rotation. It continues resolving credentials via its existing paths (`_read_nous_auth()`, `_read_codex_access_token()`, `_try_anthropic()`, etc.).

**One addition:** before resolving, check if the pool has a `last_status: "exhausted"` (within 24h) for the entry that would be resolved. If so, skip to the next available credential in the pool:

```python
# In _try_anthropic() or resolve_provider_client():
from agent.credential_pool import load_pool
pool = load_pool("anthropic")
if pool:
    entry = pool.select()  # skips exhausted entries
    if entry:
        return build_anthropic_client(entry.access_token, ...), model
# Fall through to existing resolution
```

This is ~15 lines per provider in `auxiliary_client.py`. It prevents auxiliary tasks from wasting a round-trip on a known-dead credential without requiring full pool integration.

---

## File Changes

### New Files

| File | Est. Lines | Purpose |
|------|-----------|---------|
| `agent/credential_pool.py` | ~350 | `CredentialPool` class, `load_pool()`, provider-specific entry parsing, fill-first selection, mark/rotate, persist, migration, env-var seeding, JWT label extraction |
| `hermes_cli/auth_commands.py` | ~150 | `auth add`, `auth list`, `auth remove`, `auth reset` CLI commands |

### Modified Files

| File | Change | Est. Delta |
|------|--------|-----------|
| `agent/anthropic_adapter.py` | Extract `refresh_anthropic_oauth_pure()`. Refactor `_refresh_oauth_token()` + `refresh_hermes_oauth_token()` to call it. | +40, ~15 refactored |
| `hermes_cli/auth.py` | Extract `refresh_nous_oauth_pure()`, `refresh_codex_oauth_pure()`. Add `read_credential_pool()` / `write_credential_pool()` with file-lock integration. | +100, ~25 refactored |
| `hermes_cli/runtime_provider.py` | `resolve_runtime_provider()` consults pool before falling back to existing chains. Passes pool in return dict. | +30 |
| `run_agent.py` | Accept `credential_pool` in init. Replace 3 `_try_refresh_*` methods + 3 error blocks with pool rotation + `_swap_credential()`. | -105, +60 |
| `hermes_cli/main.py` | Register `auth add/list/remove/reset` subcommands. | +10 |
| `agent/auxiliary_client.py` | Check pool `last_status` before resolving credentials in `_try_anthropic()`, `_read_nous_auth()`, `_read_codex_access_token()`. | +20 |

### Not Touched

- `gateway/run.py` — gets pool for free via `resolve_runtime_provider()` → `AIAgent`
- `config.yaml` — no new config keys
- `hermes_cli/setup.py` — existing OAuth flows reused by `hermes auth add`

### Total

~500 new lines, ~145 removed/refactored, 8 files touched.

---

## Test Plan

### Unit Tests — credential_pool.py

| # | Test | Verifies |
|---|------|----------|
| 1 | Fill-first selection returns lowest-priority non-exhausted entry | Selection strategy |
| 2 | All entries exhausted → returns None | Exhaustion boundary |
| 3 | 24h TTL: exhausted entry with old timestamp resets to "ok" | TTL expiry |
| 4 | 24h TTL: exhausted entry within window stays exhausted | TTL enforcement |
| 5 | `mark_exhausted_and_rotate()` sets status + persists + returns next | Rotation + persistence |
| 6 | `try_refresh()` success: updates token fields in-memory + disk | Refresh happy path |
| 7 | `try_refresh()` failure: marks exhausted, returns None | Refresh failure |
| 8 | JWT label extraction: valid JWT → email | Auto-labeling |
| 9 | JWT label extraction: non-JWT → None | Graceful fallback |
| 10 | Env-var seeding: creates entry, deduplicates on reload | Seeding idempotency |
| 11 | Env-var seeding: updated env var updates pool entry token | Env var rotation |
| 12 | Migration: Anthropic sources → pool entries with correct priority | Backward compat |
| 13 | Migration: Nous state → pool entry with full provider fields | Provider-specific migration |
| 14 | Migration: Codex state → pool entry | Provider-specific migration |
| 15 | Migration: idempotent (running twice doesn't duplicate) | Safety |

### Integration Tests — refresh isolation

| # | Test | Verifies |
|---|------|----------|
| 16 | Refresh Anthropic cred B does NOT overwrite cred A's token | No singleton clobbering |
| 17 | Refresh Nous cred B does NOT overwrite cred A's agent key | No singleton clobbering |
| 18 | Pool persists refresh result to `credential_pool` section only | Isolation from singleton files |
| 19 | Existing singleton refresh functions still work (backward compat) | Refactor didn't break old path |

### Integration Tests — runtime wiring

| # | Test | Verifies |
|---|------|----------|
| 20 | `resolve_runtime_provider()` returns pool credential when pool exists | Startup wire-up |
| 21 | `resolve_runtime_provider()` falls back to old chain when pool empty | Backward compat |
| 22 | Pool passed through to AIAgent via runtime dict | Rotation availability |
| 23 | Gateway creates agent with pool from `resolve_runtime_provider()` | Gateway gets rotation |

### Integration Tests — rotation flow

| # | Test | Verifies |
|---|------|----------|
| 24 | 402 on cred 1 → auto-rotate to cred 2 → success | Happy path rotation |
| 25 | 429 (first) → retry same cred → success | Transient throttle |
| 26 | 429 (first) → retry same cred → 429 again → rotate | Hard rate limit |
| 27 | All creds exhausted → `_try_activate_fallback()` | Cross-provider fallback |
| 28 | 401 → `try_refresh_current()` → swap → success | Auth refresh |
| 29 | Cross-session: exhaust in session 1, session 2 skips it | Persisted status |
| 30 | Cross-session: 24h later, session re-probes | TTL expiry |
| 31 | Auxiliary client skips known-dead credential | Auxiliary awareness |

### CLI Tests

| # | Test | Verifies |
|---|------|----------|
| 32 | `hermes auth add anthropic` (OAuth mock) → pool entry with JWT label | Registration |
| 33 | `hermes auth add anthropic --type api-key` → pool entry with manual label | API key registration |
| 34 | `hermes auth list` output format matches spec | Display |
| 35 | `hermes auth remove` removes correct entry, re-indexes priorities | Removal |
| 36 | `hermes auth reset` clears all `last_status` for provider | Manual recovery |
