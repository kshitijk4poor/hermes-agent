"""ACP projection of the profile gateway; never constructs an execution owner."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import uuid

import acp
from acp.schema import (
    AgentCapabilities, Implementation, InitializeResponse, LoadSessionResponse,
    NewSessionResponse, PromptCapabilities, PromptResponse, ResumeSessionResponse,
    SessionCapabilities, SessionResumeCapabilities,
)

from hermes_cli.gateway_client import GatewayClientError, connect_gateway
from hermes_constants import get_hermes_home
from acp_adapter.session import _translate_acp_cwd, _normalize_cwd_for_compare


class GatewayACPAgent(acp.Agent):
    def __init__(self):
        self._conn = None
        self._gateway = None
        self._connection = None
        self._connect_lock = asyncio.Lock()
        self._home = get_hermes_home().resolve()
        self._snapshots = {}
        self._event_task = None
        self._terminals = {}
        self._streamed = {}
        self._changed = asyncio.Condition()
        self._failure = None

    def on_connect(self, conn):
        self._conn = conn

    async def initialize(self, **kwargs):
        from hermes_cli import __version__
        from acp_adapter.auth import build_auth_methods
        return InitializeResponse(protocol_version=acp.PROTOCOL_VERSION,
            agent_info=Implementation(name="hermes-agent", version=__version__),
            agent_capabilities=AgentCapabilities(load_session=True,
                prompt_capabilities=PromptCapabilities(image=False),
                session_capabilities=SessionCapabilities(resume=SessionResumeCapabilities())),
            auth_methods=build_auth_methods())

    async def authenticate(self, method_id, **kwargs):
        from acp_adapter.server import HermesACPAgent
        return await HermesACPAgent.authenticate(self, method_id, **kwargs)

    async def _client(self):
        async with self._connect_lock:
            if self._gateway is None:
                connection = connect_gateway()
                gateway = await connection.__aenter__()
                try:
                    descriptor = await gateway.rpc("runtime.describe")
                    if descriptor.get("profile_id") != str(self._home):
                        raise GatewayClientError("profile_mismatch")
                except BaseException:
                    await connection.__aexit__(None, None, None)
                    raise
                self._connection, self._gateway = connection, gateway
                self._event_task = asyncio.create_task(self._events())
            if self._failure:
                raise self._failure
            return self._gateway

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        client = await self._client()
        descriptor = await client.rpc("runtime.describe")
        if ("acp" not in descriptor.get("session_create", {}).get("sources", [])
                or "acp-editor-policy-v1" not in descriptor.get("capabilities", [])):
            raise GatewayClientError("acp_policy_unavailable")
        snapshot = await client.rpc("session.create", request_id=uuid.uuid4().hex,
            source="acp", cwd=_translate_acp_cwd(cwd),
            editor={"mcp_servers": [s.model_dump(by_alias=True) for s in mcp_servers or []],
                    "edit_approval_policy": "ask"})
        self._snapshots[snapshot["session_id"]] = snapshot
        return NewSessionResponse(session_id=snapshot["session_id"])

    async def _resume(self, cwd, session_id, mcp_servers):
        if mcp_servers:
            raise GatewayClientError("acp_mcp_policy_unavailable")
        client = await self._client()
        info = await client.rpc("session.info", session_id=session_id)
        if "cwd" in info and _normalize_cwd_for_compare(info["cwd"]) != _normalize_cwd_for_compare(_translate_acp_cwd(cwd)):
            raise GatewayClientError("cwd_policy_conflict")
        if "cwd" not in info and self._conn:
            await self._conn.session_update(session_id=session_id, update=acp.update_agent_message_text(
                "Attached to the gateway's existing session policy; editor cwd is not applied.\n"))
        snapshot = await client.rpc("session.resume", session_id=session_id)
        self._snapshots[session_id] = snapshot
        from acp_adapter.server import _history_replay_updates
        if self._conn:
            for update in _history_replay_updates(snapshot["messages"]):
                await self._conn.session_update(session_id=session_id, update=update)
        return snapshot

    async def load_session(self, cwd, session_id, mcp_servers=None, **kwargs):
        await self._resume(cwd, session_id, mcp_servers)
        return LoadSessionResponse()

    async def resume_session(self, cwd, session_id, mcp_servers=None, **kwargs):
        await self._resume(cwd, session_id, mcp_servers)
        return ResumeSessionResponse()

    async def prompt(self, prompt, session_id, **kwargs):
        if session_id not in self._snapshots:
            raise GatewayClientError("not_found")
        if any(getattr(block, "type", None) != "text" for block in prompt):
            raise GatewayClientError("acp_content_unavailable")
        text = "\n".join(block.text for block in prompt)
        if text.lstrip().startswith("/"):
            raise GatewayClientError("acp_command_unavailable")
        client = await self._client()
        receipt = await client.rpc("prompt.submit", session_id=session_id,
                                   input_id=uuid.uuid4().hex, text=text)
        admission_id = receipt["admission_id"]
        async with self._changed:
            await self._changed.wait_for(lambda: admission_id in self._terminals or self._failure is not None)
            if self._failure:
                raise self._failure
            self._terminals.pop(admission_id)
        return PromptResponse(stop_reason="end_turn")

    async def _events(self):
        try:
            while True:
                frame = await self._gateway.events.get()
                if isinstance(frame, Exception):
                    raise frame
                event = frame.get("params", {})
                sid = event.get("session_id")
                snapshot = self._snapshots.get(sid)
                if snapshot is None:
                    continue
                epoch, seq = event.get("replay_epoch"), event.get("seq", 0)
                if epoch == snapshot.get("replay_epoch") and seq <= snapshot.get("last_sequence", 0):
                    continue
                snapshot.update(replay_epoch=epoch, last_sequence=seq,
                    execution_generation=event.get("execution_generation", snapshot.get("execution_generation")))
                await self._project(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._changed:
                self._failure = exc
                self._changed.notify_all()

    async def _project(self, event):
        sid, kind, payload = event["session_id"], event.get("type"), event.get("payload", {})
        aid = event.get("admission_id")
        if kind == "message.delta":
            text = payload.get("text", "")
            self._streamed[aid] = self._streamed.get(aid, "") + text
            if text and self._conn:
                await self._conn.session_update(session_id=sid, update=acp.update_agent_message_text(text))
        elif kind == "message.complete":
            text = payload.get("text", "")
            prefix = self._streamed.pop(aid, "")
            remainder = text[len(prefix):] if text.startswith(prefix) else text
            if remainder and self._conn:
                await self._conn.session_update(session_id=sid, update=acp.update_agent_message_text(remainder))
            async with self._changed:
                self._terminals[aid] = payload
                if len(self._terminals) > 256:
                    self._terminals.pop(next(iter(self._terminals)))
                self._changed.notify_all()

    async def aclose(self):
        if self._event_task:
            self._event_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._event_task
        if self._connection:
            await self._connection.__aexit__(None, None, None)
            self._connection = None
        self._gateway = None
