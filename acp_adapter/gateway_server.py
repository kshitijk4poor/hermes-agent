"""ACP projection of the profile gateway; never constructs an execution owner."""
from __future__ import annotations

import asyncio
import uuid

import acp
from acp.schema import NewSessionResponse

from hermes_cli.gateway_client import GatewayClientError, connect_gateway
from hermes_constants import get_hermes_home
from acp_adapter.session import _translate_acp_cwd


class GatewayACPAgent(acp.Agent):
    def __init__(self):
        self._conn = None
        self._gateway = None
        self._connection = None
        self._connect_lock = asyncio.Lock()
        self._home = get_hermes_home().resolve()
        self._snapshots = {}

    def on_connect(self, conn):
        self._conn = conn

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
            return self._gateway

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        client = await self._client()
        descriptor = await client.rpc("runtime.describe")
        if ("acp" not in descriptor.get("session_create", {}).get("sources", [])
                or "acp-editor-policy-v1" not in descriptor.get("capabilities", [])):
            raise GatewayClientError("acp_policy_unavailable")
        # This is a closed, server-validated launch envelope, not a CLI relabel.
        snapshot = await client.rpc("session.create", request_id=uuid.uuid4().hex,
            source="acp", cwd=_translate_acp_cwd(cwd),
            editor={"mcp_servers": [s.model_dump(by_alias=True) for s in mcp_servers or []],
                    "edit_approval_policy": "ask"})
        self._snapshots[snapshot["session_id"]] = snapshot
        return NewSessionResponse(session_id=snapshot["session_id"])
