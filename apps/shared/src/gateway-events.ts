/**
 * Wire types for the `tui_gateway` JSON-RPC surface shared by the Ink TUI, Desktop and the
 * web dashboard.
 *
 * Every shape here is GENERATED from `tui_gateway/contracts` (Python is the single source):
 * `./gateway-contract.generated.ts` carries `RpcMethods` (client→server method → params/result),
 * `ServerRequestMap` (server→client request → params/result), `GatewayEventMap` (notification
 * type → payload) and every value shape. `scripts/gen_gateway_contracts.py` regenerates it and
 * `tests/tui_gateway/contracts/test_generated.py` fails when the committed file is stale, so a field the
 * backend stops sending fails `tsc` here instead of drifting.
 *
 * This module adds only what the wire does not carry: the client-local synthetic events the TUI
 * transport publishes into the same handler stream, and the `GatewayEvent` envelope.
 */
import type { BackendGatewayEventMap } from './gateway-contract.generated.js'

export * from './gateway-contract.generated.js'

/**
 * Client-local synthetic events. Never emitted by `tui_gateway`; the Ink TUI's `gatewayClient`
 * publishes them into the same handler stream to report transport state.
 */
export interface ClientLocalGatewayEventMap {
  'dashboard.new_session_requested': { reason?: string }
  'gateway.protocol_error': { preview?: string }
  'gateway.reconnecting': { attempt?: number; delay_ms?: number }
  'gateway.start_timeout': { cwd?: string; python?: string; stderr_tail?: string }
  'gateway.stderr': { line: string }
  /** Synthetic on the client (a reconnect replay could not cover the gap: epoch changed /
   * ring truncated) AND emitted by canonical gateways on fanout overflow
   * (`gateway/session_events.py`); consumers re-resume the session for a snapshot. */
  'session.replay_gap': { latest_seq?: number; replay_epoch?: string }
}

export interface GatewayEventMap extends BackendGatewayEventMap, ClientLocalGatewayEventMap {}

export type GatewayEventName = keyof GatewayEventMap

/** One `event` notification's `params`. */
export interface GatewayEvent<K extends GatewayEventName = GatewayEventName> {
  /** Client-local: recovered/held during reconnect, not fresh user-facing work. */
  replayed?: boolean
  /** Client-local: the backend process's `replay_epoch` the delivering socket had adopted
   * (from `gateway.ready`) when it dispatched this event. Two sockets to one process share
   * it, so a renderer can recognise the same frame arriving on both. */
  replayEpoch?: string
  /** Registry connection whose socket delivered the event (renderer-side tag;
   * absent for the local/legacy primary path). */
  connectionId?: string
  /** Owner execution stamp on canonical gateways: the integer runtime epoch and the claimed
   * generation, spread onto the params beside `type`/`payload` (`gateway/session_events.py`). */
  authority_epoch?: number
  execution_generation?: number
  /** Session-scoped replay generation on canonical gateways. */
  replay_epoch?: string
  payload?: GatewayEventMap[K]
  /** Renderer-side source tag added by the Desktop gateway registry. */
  profile?: string
  /** Per-session monotonic counter stamped by `tui_gateway/event_replay.py::_stamp_event`;
   *  absent on session-less broadcasts. */
  seq?: number
  session_id?: string
  type: K
}

// ── RPC responses shared across surfaces ─────────────────────────────

/** `hermes_cli/inventory.py` one `model.options` provider row (union of every field the
 *  backend sets; `pricing_pending` / `free_tier_pending` mark the cached-only fail-closed path). */
export interface ModelOptionProvider {
  /** User-defined providers only: every accepted identity for this endpoint
   *  (bare config key, `custom:<key>`, normalized display name, …). A session's
   *  `model.options` reports the canonical `custom:<key>` form, so "is this row
   *  the current provider?" must check membership here, not slug equality. */
  aliases?: string[]
  /** OpenAI-compatible endpoint for a user-defined provider. The backend
   *  exposes this as `api_url`; model assignments send it back as `base_url`. */
  api_url?: string
  /** Auth flow for an unconfigured provider: "api_key" can be activated inline
   *  by pasting `key_env`; anything else (oauth_*, external, aws_sdk, …) needs
   *  the `hermes model` CLI / onboarding OAuth flow. */
  auth_type?: string
  /** True when the provider has usable credentials. False for canonical
   *  providers surfaced by `include_unconfigured` that the user hasn't set up
   *  yet — render these with a setup affordance instead of hiding them. */
  authenticated?: boolean
  /** Per-model option support, keyed by model id (present when the picker
   *  requested capabilities). Lets the UI gate fast/reasoning controls. */
  capabilities?: Record<string, ModelCapabilities>
  /** Curated shortlist (one flagship per lab) the picker shows by default for
   *  aggregator providers that serve dozens of models across many labs. */
  featured_models?: string[]
  /** Nous only: whether the current account is on the free plan. */
  free_tier?: boolean
  /** Nous only, cached-only inventory: entitlement unknown, every model rendered locked. */
  free_tier_pending?: boolean
  /** True for the free-tier route's own provider row (no account behind it).
   *  Never match this row by `name` — the label is copy and can change. */
  free_tier_row?: boolean
  is_current?: boolean
  /** True for providers defined via the user's `providers:` config block. */
  is_user_defined?: boolean
  /** Env var to paste an API key into, for unconfigured `api_key` providers. */
  key_env?: string
  models?: string[]
  name: string
  /** Per-model pricing keyed by model id (present when the picker requested
   *  pricing and the provider supports live pricing). */
  pricing?: Record<string, ModelPricing>
  /** Cached-only inventory: pricing not fetched yet. */
  pricing_pending?: boolean
  slug: string
  source?: string
  total_models?: number
  /** Nous only: paid models a free-tier user cannot select (shown disabled). */
  unavailable_models?: string[]
  warning?: string
}

export interface ModelPricing {
  /** Formatted $/Mtok cached-input price, or null when the model has none. */
  cache: null | string
  /** Sale: rounded percent off list when gateway sends pricing.original. */
  discount_percent?: number
  /** True when the model costs nothing (free tier eligible). */
  free: boolean
  /** Formatted $/Mtok input price, e.g. "$3.00", or "free", or "" if unknown. */
  input: string
  /** Formatted $/Mtok output price. */
  output: string
  /** Sale: formatted pre-discount input $/Mtok ("was"). */
  was_input?: string
  /** Sale: formatted pre-discount output $/Mtok ("was"). */
  was_output?: string
}

export interface ModelCapabilities {
  /** False when the route rejects a reasoning disable ("mandatory" in the
   *  provider catalog), so the Thinking toggle must not be offered. */
  can_disable_reasoning?: boolean
  fast: boolean
  reasoning: boolean
}

export interface ModelOptionsResponse {
  model?: string
  provider?: string
  providers?: ModelOptionProvider[]
}

/** `tui_gateway/methods_session.py::_session_row_summary` — one `session.list` row. */
export interface SessionListItem {
  id: string
  message_count: number
  preview: string
  /** The runtime id this stored session is currently attached to, when live. */
  resolved_id?: string
  source?: string
  started_at: number
  title: string
}

export interface SessionListResponse {
  sessions?: SessionListItem[]
}

/** Transcript row as projected by the gateway (`session.resume` / `session.activate`). */
export interface GatewayTranscriptMessage {
  args?: unknown
  context?: string
  display_kind?: string
  display_metadata?: unknown
  name?: string
  role: 'assistant' | 'system' | 'tool' | 'user'
  text?: string
}

export interface SessionInflightTurn {
  assistant?: string
  correction_offsets?: number[]
  corrections?: string[]
  error?: string
  error_surface?: ErrorSurface
  recoverable?: boolean
  status?: string
  streaming?: boolean
  user?: string
}

/** `tui_gateway/methods_session.py::_resume_response`. `info` is surface-specific
 *  (`SessionInfo` in the TUI, `SessionRuntimeInfo` on Desktop); narrow at the call site. */
export interface SessionResumeResponse<Info = Record<string, unknown>, Message = GatewayTranscriptMessage> {
  /** Present when the backend found a fresh crash-interrupted turn and scheduled its
   *  automatic continuation; the turn arrives as a normal message.start stream. */
  auto_continue?: { attempt: number; interrupted_at: number }
  /** Deferred hydration: history arrives via `session.resume_progress`. */
  hydrating?: boolean
  inflight?: null | SessionInflightTurn
  info?: Info
  message_count?: number
  messages: Message[]
  /** `omit_messages` resume: the client still learns the stored size. */
  messages_omitted?: boolean
  resumed?: string
  running?: boolean
  session_id: string
  session_key?: string
  started_at?: number
  status?: string
  /** Canonical gateways: the durable id of the row this live session projects. */
  stored_session_id?: string
  /** Canonical gateways: the actor's subscription token for `session.detach`. */
  subscription_id?: string
  todo_state?: TodoStatePayload
}
