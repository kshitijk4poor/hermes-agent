# Canonical local gateway protocol

Native local connections use the ordinary gateway owner. Closing Desktop,
changing profile or evicting a viewer never transfers that owner into Electron's
child-process pool. Local ensure retains the update gate and rechecks profile
retirement after asynchronous waits. Remote SSH, URL and OAuth routing retain
their existing authentication paths; failure never creates a local replacement.

## Authentication

`gateway ensure --json` returns a credential-free endpoint. Native main requests
private `session-ticket` grants with exactly `profile_id`, `instance_id` and
`purpose`. WebSocket grants use `interactive`; JSON HTTP requests use
`native-http`. Each HTTP retry mints a fresh grant and sends it only in
`X-Hermes-Gateway-Ticket`, scoped to the endpoint's exact origin. HTTP credentials
are not stored in the renderer or attached to URLs/public connection descriptors.

## Session and input identities

`src/api/canonical-protocol.ts` adapts only native canonical sockets. Creation
uses source `gui`, not `cli`. The foreground draft retains its `request_id` after
an uncertain create ACK; New chat starts a new intent. Unsupported explicit
launch settings are rejected visibly rather than silently dropped. Renderer
routing profile, terminal column count and disabled fast mode are not execution
policy overrides. This does not provide a crash-durable create-intent journal.

The canonical `admission_id` is distinct from the submitted input ID. Receipt
projection retains both and verifies `ref.session_id` against the requested
session before acknowledging the prepared input. The existing native private
atomic-file journal is still written before transport admission and retired only
after a matching accepted receipt. An unknown receipt retains it for explicit
retry. Renderer-only localStorage recovery is not native crash durability.

## Controls and metadata

Live and replayed prompt projections retain `prompt_id` and execution generation,
including named event listeners. Stop uses the captured session generation;
approval and clarify responses resolve the exact projected prompt, never the
latest arbitrary prompt. Resume restores pending approval/clarify projections.

Canonical `session.title` and `session.archive` RPC requests become
`session.mutate` with a retained request ID and expected revision. An ambiguous
failure retains that CAS payload; an explicit revision conflict requires a fresh
snapshot before retry. Existing REST metadata writers are not migrated by this
RPC adapter and do not yet provide universal revision fencing.

## Cutover limits

The canonical authority currently exposes a narrower RPC set than the full
Desktop application. Unsupported model/tool/slash/session-management RPCs,
cross-window pending-input text projection, non-JSON download authentication,
Windows validated named-pipe bootstrap, and WSL topology require further
integration. Successful authenticated transport is not proof of a usable mounted
chat or native journal retirement. End-to-end acceptance must compose the HTTP
grant server and drive real Electron controls with disposable userData.
