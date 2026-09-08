# Gateway-owned fresh local sessions

The composed gateway listener can create a fresh local **CLI-policy** session without
starting `tui_gateway`'s separate agent runtime. The existing `GatewayRunner` / `TurnRunner`
own execution, approval waiters, tool calls, session storage, and canonical event delivery.

A server-authenticated identity with `session:create` can call:

```json
{"jsonrpc":"2.0","id":1,"method":"session.create","params":{"request_id":"stable-client-create-id","source":"cli"}}
```

The result preserves `session_id`, `stored_session_id`, `messages`, `message_count`, and
`info`, and includes the authority's normal resume snapshot fields. Both session IDs are
the same persisted ID. Creation is lazy with respect to the agent, but reserves the real
SessionDB/SessionStore identity before returning. Repeating the request ID for the same
principal and profile reuses its persisted routing entry. Omit the ID for a new session;
clients that need lost-ACK retry protection must retain it.

Attach another authenticated viewer with `session.resume({session_id})`, and submit
through `prompt.submit({session_id, submission_id, text})`. The accepted receipt is durable
admission, not inference completion. Closing every viewer leaves the execution and its
pending approval alive. A new viewer can resume the same identity and answer through
`approval.respond({session_id, execution_generation, prompt_id, choice})`.

## Deliberately limited compatibility

- Only `source: "cli"` (also the default) is currently accepted. It maps to the existing
  `Platform.LOCAL` → CLI tool/display policy. GUI/TUI policy is **not** inferred from
  process environment or the identity of an attaching viewer. Explicit GUI/TUI/native/
  automation source selections are rejected until their own policy is wired and tested.
- Creation currently accepts only `request_id` and `source`. Model/provider/reasoning,
  cwd/worktree, toolsets/skills, seeded history, profile switching, YOLO, and other launch
  options return `invalid_params`; none silently modify daemon-wide configuration.
- Fresh creation currently requires the identity stamped by the existing gated WS ticket
  path. The legacy ungated `?token=` path in this base does not stamp identity and cannot
  create. The authentication/bootstrap integration must supply the proper server principal;
  callers cannot provide one in RPC parameters. Resume-only capabilities must not be
  promoted to `session:create` by that integration.
- `session.list({limit})` returns authorized **live** authority sessions (`scope: "live"`),
  not the full historical session picker. `session.info({session_id})` is a lightweight,
  authorized view of source, current model, lazy-agent status, and profile ID.
- `ping({})` returns `{pong: true}`. `runtime.describe({})` exposes only authority identity,
  epoch, and the narrow implemented creation contract. It does not claim full runtime
  protocol readiness, reveal credentials/paths, start an agent, or renew a turn lease.
- Reconnect while the owner remains alive is covered. Cold gateway restart attachment,
  reset/compression identity migration, crash recovery of local queued work, and atomic
  general creation-policy receipts remain separate integration work. Stable routing is
  not a claim of the plan's complete transactional mutation/create API.

## Verification

`tests/gateway/test_local_session.py` launches a disposable process with temporary HOME
and HERMES_HOME, the production composed HTTP/WS listener, real single-use authenticated
tickets, a loopback OpenAI-compatible model, the real TurnRunner/AIAgent, and the real
terminal tool. It proves persisted identity and same-agent reattachment; closes both
viewers while approval is pending; reconnects and consents before deleting only an owned
temporary directory; checks canonical history; and rejects forged authentication/source/
profile and unsupported launch fields. No native messaging allow-all credential is used.
