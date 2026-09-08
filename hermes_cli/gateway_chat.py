"""Normal classic/one-shot launch through the canonical gateway, never AIAgent."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import uuid

from hermes_cli.gateway_client import GatewayClientError, connect_gateway

# These options change execution or require frontend facilities not yet exposed by
# the authority. Reject them, rather than mutate process-wide gateway settings.
_UNSUPPORTED = (
    "image", "skills", "worktree", "w", "checkpoints", "pass_session_id",
    "ignore_user_config", "safe_mode", "yolo", "accept_hooks",
    "continue_last", "create_if_missing", "no_restore_cwd", "usage_file",
    "run_budget", "verbose", "compact",
    "list_tools", "list_toolsets",
)
_POLICY = ("model", "provider", "reasoning", "toolsets", "max_turns", "base_url", "ignore_rules", "api_key")


def validate_options(args):
    unsupported = [name for name in _UNSUPPORTED if getattr(args, name, None)]
    if getattr(args, "resume", None) == "latest":
        unsupported.append("resume latest")
    if unsupported:
        flags = ", ".join("--" + name.replace("_", "-") for name in unsupported)
        raise GatewayClientError(f"Unsupported gateway CLI options: {flags}. No local fallback or policy changes were made.")
    if getattr(args, "resume", None) and (getattr(args, "in_dir", None) or getattr(args, "source", None) or
            any(getattr(args, name, None) not in (None, False) for name in _POLICY)):
        raise GatewayClientError("Resume retains gateway session policy; creation overrides are unsupported on resume.")


async def run_gateway_chat(args):
    from hermes_cli.gateway_chat_view import GatewayChatView
    async with connect_gateway() as client:
        description = await client.rpc("runtime.describe")
        if getattr(args, "resume", None):
            snapshot = await client.rpc("session.resume", session_id=args.resume)
        else:
            contract = description.get("session_create", {})
            source = getattr(args, "source", None) or "cli"
            if source not in contract.get("sources", []):
                raise GatewayClientError(f"Gateway does not support source {source!r}")
            parameters = contract.get("parameters", [])
            policy = {key: getattr(args, key) for key in _POLICY if getattr(args, key, None) is not None}
            if isinstance(policy.get("toolsets"), str):
                policy["toolsets"] = [name.strip() for name in policy["toolsets"].split(",") if name.strip()]
            cwd = str(Path(getattr(args, "in_dir", None) or os.getcwd()).expanduser().resolve())
            if "cwd" in parameters:
                policy["cwd"] = cwd
            elif getattr(args, "in_dir", None):
                raise GatewayClientError("Gateway does not support --in / caller cwd; update the gateway")
            else:
                print("Warning: this gateway cannot preserve caller cwd; it uses its configured execution directory.", file=sys.stderr)
            missing = sorted(set(policy) - set(parameters))
            if missing:
                raise GatewayClientError("Gateway does not support creation options: " + ", ".join(missing))
            snapshot = await client.rpc("session.create", request_id=uuid.uuid4().hex, source=source, **policy)
        print("Session: " + snapshot["stored_session_id"], file=sys.stderr, flush=True)
        query = getattr(args, "query", None) or getattr(args, "q", None)
        oneshot_prompt = getattr(args, "oneshot", None)
        if isinstance(oneshot_prompt, str):
            query = oneshot_prompt
        quiet = bool(getattr(args, "quiet", False) or oneshot_prompt)
        oneshot = bool(oneshot_prompt or getattr(args, "oneshot_exit", False) or quiet or
                       (query and not (sys.stdin.isatty() and sys.stdout.isatty())))
        view = GatewayChatView(client, snapshot, quiet=quiet)
        if getattr(args, "resume", None) and not quiet:
            for row in snapshot.get("messages", []):
                if row.get("role") in {"user", "assistant"} and isinstance(row.get("content"), str):
                    print(f"{row['role']}: {row['content']}")
        return await view.run(query, oneshot=oneshot)


def launch_from_args(args) -> int:
    from websockets.exceptions import WebSocketException
    try:
        validate_options(args)
        from hermes_cli.gateway_chat_startup import ensure_launch_provider
        if not ensure_launch_provider(args):
            return 0
        query_file = getattr(args, "query_file", None)
        if query_file:
            args.query = sys.stdin.read() if query_file == "-" else Path(query_file).read_text(encoding="utf-8")
            if not args.query.strip():
                raise GatewayClientError("--query-file is empty")
        if not (getattr(args, "query", None) or getattr(args, "q", None) or getattr(args, "oneshot", None) or sys.stdin.isatty()):
            raise GatewayClientError("Noninteractive chat requires --query or --oneshot")
        return asyncio.run(run_gateway_chat(args))
    except (GatewayClientError, OSError, TimeoutError, WebSocketException) as exc:
        # WebSocket errors can embed credential URLs/remote bodies.
        message = str(exc) if isinstance(exc, GatewayClientError) else "Gateway connection/read failed; no local fallback"
        print("Error: " + message, file=sys.stderr)
        return 2 if isinstance(exc, GatewayClientError) and ("Unsupported" in message or "unsupported" in message) else 1
    except KeyboardInterrupt:
        print("Detached; accepted work continues at the gateway.", file=sys.stderr)
        return 130


def launch_from_kwargs(options) -> int:
    return launch_from_args(argparse.Namespace(**options))
