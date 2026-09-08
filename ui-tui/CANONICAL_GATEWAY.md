# Ink canonical local gateway connection

Normal local Ink startup now ensures the profile's ordinary gateway through
`ensure_gateway_runtime`, obtains an instance/profile-bound private control ticket,
and connects over authenticated WebSocket. The short Python bootstrap subprocess
is only a client; Ink no longer starts `tui_gateway.entry` or owns an agent loop.
The ticket travels through a private child pipe and WebSocket subprotocol, never
in an endpoint URL or discovery receipt. Explicit remote URLs remain remote: a
failed connection cannot fall back to a local daemon.

Closing Ink detaches its socket without stopping the gateway. Reconnection gets
a fresh single-use ticket using discovery only; it cannot resurrect an explicitly
stopped gateway. An initial ensure failure is visible and does not create a rival
owner. Local profile selection remains the launcher's exact effective HERMES_HOME.

Ink requires `runtime.describe.session_create` to advertise the `tui` source and
every requested creation parameter. It sends a fresh `request_id`, `source: tui`,
and explicit launcher options; an older CLI-only runtime returns a visible policy
compatibility failure rather than silently creating CLI-policy work. Attach to an
existing session does not apply the viewer's creation options.

Prepared submission IDs and captured destination fences remain authoritative.
The wire adapter maps `submission_id` to canonical `input_id` and translates the
receipt's `ref` into the existing journal acknowledgement shape. Resume/create
snapshots and events retain the authority epoch and execution generation. Shared
approval/clarify responses carry the actual prompt ID and generation; a resumed
snapshot restores its pending prompt cards. Stop includes the active generation.

## Current gaps

- The baseline runtime supports only CLI creation; fresh TUI creation needs the
  separately integrated TUI launch-policy support. No CLI impersonation is used.
- Runtime descriptions and session metadata are intentionally narrow. Ink renders
  an explicit unavailable-inventory message, not fabricated tool/skill lists.
- Most legacy rich UI RPCs (config, wake, slash execution, uploads, historical
  pickers, metadata editing, and subagents) are not exposed by this authority yet;
  they fail visibly. This is not complete feature parity.
- A per-view session switch does not terminate the old shared session. The present
  authority removes subscriptions when the socket closes, so old subscriptions
  can remain until that detach; the renderer still filters by current session.
- Secret/sudo and batch clarification are not claimed supported. Cold owner
  restart recovery is outside this scoped migration.

## Native verification

`python ui-tui/scripts/native_canonical_probe.py <receipt-directory>` uses a
private temporary HOME/HERMES_HOME, an ordinary no-platform daemon, a loopback
OpenAI-compatible model, and real Ink processes on native Linux PTYs. It creates
an explicitly labelled seed session with the runtime's supported policy, resumes
it from two Ink viewers, submits through the real renderer, checks both rendered
replies, detaches both viewers, and resumes persisted history in a new Ink process.
The fixture does not replace renderer predicates or inject RPC results.
