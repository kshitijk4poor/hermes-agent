"""ACP controls retain generation fences and reject unavailable mutations."""
import pytest

from acp_adapter.server import HermesACPAgent
from hermes_cli.gateway_client import GatewayClientError
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_cancel_uses_observed_generation_and_mutations_fail_explicitly(tmp_path):
    agent = HermesACPAgent()
    calls = []

    class RPC:
        async def rpc(self, method, **params):
            calls.append((method, params))
            raise GatewayClientError("stale_generation")

    agent._gateway = RPC()
    agent._snapshots["shared"] = {"execution_generation": 4, "revision": 7}
    with pytest.raises(GatewayClientError, match="stale_generation"):
        await agent.cancel("shared")
    assert calls == [("session.interrupt", {"session_id": "shared", "execution_generation": 4})]
    for method, params in ((agent.fork_session, {"session_id": "shared", "cwd": str(tmp_path)}),
                           (agent.set_session_model, {"session_id": "shared", "model_id": "new-model"}),
                           (agent.set_session_mode, {"session_id": "shared", "mode_id": "dont_ask"})):
        with pytest.raises(GatewayClientError, match="unavailable"):
            await method(**params)
    assert len(calls) == 1, "Unsupported operations must not reach an alternate writer"


@pytest.mark.asyncio
async def test_gateway_acp_catalog_does_not_boot_a_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = HermesACPAgent()
    assert (await agent.list_sessions()).sessions == []
    assert not (tmp_path / "state.db").exists()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(session_id="persisted-acp", source="acp", model="fixture", model_config={"cwd": str(tmp_path)})
        db.append_message("persisted-acp", "user", "picker history")
    assert [row.session_id for row in (await agent.list_sessions(cwd=str(tmp_path))).sessions] == ["persisted-acp"]
    assert agent._gateway is None
