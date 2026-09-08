# Gateway-owned fresh local sessions

The composed gateway listener can create fresh local **CLI, TUI, or GUI-policy** sessions without
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

- `source` accepts `cli` (default), `tui`, or `gui`. These select the existing agent
  platforms `cli`, `tui`, and `desktop`, respectively. The native local routing identity
  remains server-owned `Platform.LOCAL`; source never grants messaging/native trust.
  Default TUI selection folds in `project`; GUI folds in `project` and `desktop_ui`.
  GUI policy is independent of `HERMES_DESKTOP` and of the attaching viewer's identity.
- Optional flat creation fields: `cwd` (existing absolute gateway-local directory),
  `model` (nonempty model identifier on the daemon's configured provider), and `toolsets`
  (explicit array of established toolset names, including an empty array). Unknown or
  policy-filtered toolsets reject instead of silently disappearing. Explicit toolsets
  override the default surface additions; CLI/TUI cannot explicitly request `desktop_ui`.
  `cwd`, model selection, source and effective toolsets are captured at creation; attach
  cannot change them. A conflicting repeat `request_id` returns `invalid_params`.
- Provider/base URL overrides, reasoning/service-tier, skills, cwd worktree creation,
  seeded history, profile switching, YOLO, and other launch options still return
  `invalid_params`. Provider credentials/routing and reasoning/service-tier defaults still
  use the existing gateway resolution lifecycle; this is not full launch-option parity.
  No supported launch field mutates daemon-wide configuration or process environment.
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
profile and unsupported launch fields. A second case runs the real clarify tool,
reattaches after all viewers close, and verifies the answer reaches the next loopback
model request. No native messaging allow-all credential authorizes the execution; a
separate negative control enables messaging allow-all only while proving reconstructed
local source/profile objects still fail closed.

`tests/gateway/test_session_policy.py` adds three simultaneous CLI/TUI/GUI turns against
an owned loopback model. A barrier holds their first requests concurrently; real terminal
calls write separate files in three owned working directories. The next requests expose
the correct cwd results, distinct requested models and surface-specific tool schemas.
Reconnect preserves each agent; launcher environment carriers and config bytes do not
change. This is loopback integration evidence, not native launcher or vendor evidence.
The frozen policy is currently adapter-owned memory; restart integration must persist and
restore it before execution. A registered local source missing its policy fails closed.
