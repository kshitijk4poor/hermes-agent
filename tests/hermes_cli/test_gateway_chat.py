"""Client launch must not silently downgrade policy or own execution."""
import argparse

import pytest


def test_unsupported_launch_options_fail_before_connection(monkeypatch, capsys):
    from hermes_cli import gateway_chat
    calls = []
    monkeypatch.setattr(gateway_chat, "connect_gateway", lambda: calls.append(True))
    for option in ("yolo", "ignore_rules", "safe_mode", "worktree", "continue_last", "usage_file"):
        args = argparse.Namespace(**{option: True})
        assert gateway_chat.launch_from_args(args) == 2
        assert option.replace("_", "-") in capsys.readouterr().err
    assert calls == []


@pytest.mark.asyncio
async def test_rpc_preserves_notifications_and_errors():
    import asyncio
    import json
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect
    from hermes_cli.gateway_client import GatewayClient, GatewayClientError

    async def peer(ws):
        async for raw in ws:
            request = json.loads(raw)
            await ws.send(json.dumps({"method": "message.complete", "params": {"text": "reply"}}))
            await ws.send(json.dumps({"id": request["id"], "error": {"message": "stale_generation"}}))

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            async with GatewayClient(ws) as client:
                with pytest.raises(GatewayClientError, match="stale_generation"):
                    await client.rpc("session.interrupt", session_id="stored", execution_generation=4)
                event = await asyncio.wait_for(client.events.get(), 2)
                assert event["params"]["text"] == "reply"
