from types import SimpleNamespace
from unittest.mock import patch

from agent.claude_cli_client import ClaudeCLIClient


def _completed(stdout: str):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def test_first_call_uses_session_id_then_resume_on_followup():
    client = ClaudeCLIClient(command="claude", session_id="hermes-session-1")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return _completed('{"result":"first","session_id":"claude-session-abc","usage":{"input_tokens":10,"output_tokens":5}}')
        return _completed('{"result":"second","session_id":"claude-session-abc","usage":{"input_tokens":8,"output_tokens":4}}')

    with patch("agent.claude_cli_client.subprocess.run", side_effect=fake_run):
        client.chat.completions.create(model="claude-cli/claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}])
        client.chat.completions.create(model="claude-cli/claude-sonnet-4-6", messages=[{"role": "user", "content": "again"}])

    assert "--session-id" in calls[0]
    assert "--resume" not in calls[0]
    assert "--resume" in calls[1]
    assert "claude-session-abc" in calls[1]
