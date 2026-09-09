"""Actual ACP stdio and WS clients against an ordinary isolated gateway."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from websockets.asyncio.client import connect

from hermes_cli.gateway_client import GatewayClient
from tests.gateway.test_normal_runtime_boot import control, model_peer  # noqa: F401


@pytest.fixture
def daemon(tmp_path, model_peer):
    home = tmp_path / "state"
    home.mkdir(mode=0o700)
    user = tmp_path / "user"
    user.mkdir()
    root = Path(__file__).resolve().parents[2]
    model_url = f"http://127.0.0.1:{model_peer.server_port}/v1"
    (home / "config.yaml").write_text(json.dumps({
        "gateway": {"multiplex_profiles": False},
        "approvals": {"mode": "manual", "timeout": 60},
        "model": {"provider": "custom", "default": "local-wire-stub", "base_url": model_url},
        "auxiliary": {"title_generation": {"enabled": False}},
    }))
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               PYTHONPATH=str(root), PYTHONUNBUFFERED="1",
               OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=model_url,
               HERMES_ACP_SKIP_CONFIGURED_MCP="1")
    log_path = tmp_path / "gateway.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "gateway.run"], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            descriptor = {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, "identify")
                    if descriptor.get("state") == "ready":
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            assert descriptor.get("state") == "ready", log_path.read_text()
            yield home, descriptor, env, root
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@asynccontextmanager
async def viewer(daemon):
    home, descriptor, _, _ = daemon
    grant = control(home, "session-ticket", {"profile_id": str(home),
                    "instance_id": descriptor["instance_id"], "purpose": "interactive"})
    url = descriptor["api_origin"].replace("http:", "ws:") + "/api/ws"
    async with connect(url, subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket." + grant["ticket"]]) as ws:
        async with GatewayClient(ws) as client:
            yield client


class ACPPeer:
    def __init__(self, process):
        self.process = process
        self.frames = []
        self.requests = {}
        self.serial = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        while line := await self.process.stdout.readline():
            frame = json.loads(line)
            self.frames.append(frame)
            future = self.requests.get(frame.get("id"))
            if future and ("result" in frame or "error" in frame):
                future.set_result(frame)

    async def rpc(self, method, **params):
        self.serial += 1
        rid = self.serial
        future = asyncio.get_running_loop().create_future()
        self.requests[rid] = future
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode() + b"\n")
        await self.process.stdin.drain()
        return await asyncio.wait_for(future, 40)


@asynccontextmanager
async def editor(daemon, tmp_path):
    _, _, env, root = daemon
    with (tmp_path / "acp.log").open("w") as log:
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "acp_adapter.entry",
            cwd=root, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log)
        peer = ACPPeer(process)
        try:
            yield peer
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            await peer.reader


@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_acp_transport_shares_canonical_history_and_order(daemon, tmp_path, model_peer):
    async with viewer(daemon) as ws:
        created = await ws.rpc("session.create", request_id="shared-acp", source="cli", cwd=str(tmp_path))
        sid = created["session_id"]
        async with editor(daemon, tmp_path) as acp:
            init = await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
            assert "result" in init, init
            loaded = await acp.rpc("session/load", cwd=str(tmp_path), sessionId=sid, mcpServers=[])
            assert "result" in loaded, loaded
            reply = await acp.rpc("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "WS_SHARED ACP"}])
            assert reply.get("result", {}).get("stopReason") == "end_turn", reply
            assert "LOCAL_ACK_WS_SHARED" in json.dumps(acp.frames)
        accepted = await ws.rpc("prompt.submit", session_id=sid, input_id="after-acp-disconnect", text="WS_SHARED WS")
        async with asyncio.timeout(30):
            while True:
                receipt = await ws.rpc("prompt.receipt", session_id=sid, admission_id=accepted["admission_id"])
                if receipt["status"] == "terminal":
                    assert receipt["outcome"] == "completed", receipt
                    break
                await asyncio.sleep(.05)
        snapshot = await ws.rpc("session.resume", session_id=sid)
        assert "WS_SHARED ACP" in json.dumps(snapshot["messages"])
        assert "WS_SHARED WS" in json.dumps(snapshot["messages"])
        replay = await ws.rpc("session.events.since", session_id=sid, replay_epoch=created["replay_epoch"], last_sequence=0)
        events = replay["events"]
        assert [e["seq"] for e in events] == sorted({e["seq"] for e in events})
        assert len([e for e in events if e["type"] == "message.complete"]) == 2
        assert len(model_peer.requests) == 2
        print("ACP_SHARED_RECEIPT=" + json.dumps({"session_id": sid, "events": events, "messages": snapshot["messages"]}))



@pytest.mark.linux_only
@pytest.mark.asyncio
async def test_acp_permission_detach_keeps_canonical_waiter(daemon, tmp_path, model_peer):
    import shlex
    from contextlib import suppress
    from hermes_cli.gateway_client import GatewayClientError
    from tests.gateway.fixtures.authority_controls_peer import ModelPeer as ApprovalPeer

    target = tmp_path / "owned-removal"
    target.mkdir()
    (target / "owned.txt").write_text("disposable")
    model_peer.command = "rm -r -- " + shlex.quote(str(target))
    model_peer.RequestHandlerClass = ApprovalPeer
    async with viewer(daemon) as ws:
        created = await ws.rpc("session.create", request_id="acp-permission", source="cli", cwd=str(tmp_path))
        sid = created["session_id"]
        async with editor(daemon, tmp_path) as acp:
            await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
            assert "result" in await acp.rpc("session/load", cwd=str(tmp_path), sessionId=sid, mcpServers=[])
            prompt_task = asyncio.create_task(acp.rpc("session/prompt", sessionId=sid,
                prompt=[{"type": "text", "text": "Remove the owned fixture"}]))
            try:
                async with asyncio.timeout(15):
                    while not any(f.get("method") == "session/request_permission" for f in acp.frames):
                        await asyncio.sleep(.05)
                permission = next(f for f in acp.frames if f.get("method") == "session/request_permission")
                assert target.exists()
                snapshot = await ws.rpc("session.resume", session_id=sid)
                pending = snapshot["prompts"][0]
                with pytest.raises(GatewayClientError, match="stale_generation"):
                    await ws.rpc("approval.respond", session_id=sid, prompt_id=pending["prompt_id"],
                        execution_generation=pending["execution_generation"] + 1, choice="once")
                assert target.exists()
            finally:
                prompt_task.cancel()
                with suppress(asyncio.CancelledError):
                    await prompt_task
        assert target.exists(), "Editor detach must not synthesize approval or denial"
        response = await ws.rpc("approval.respond", session_id=sid, prompt_id=pending["prompt_id"],
            execution_generation=pending["execution_generation"], choice="once")
        assert response["status"] == "resolved", response
        async with asyncio.timeout(20):
            while target.exists():
                await asyncio.sleep(.05)
        print("ACP_PERMISSION_RECEIPT=" + json.dumps({"session_id": sid, "permission": permission,
              "stale_generation_rejected": True, "detached_waiter_resolved_by_ws": True,
              "real_owned_deletion": not target.exists()}))
