"""Provider-specific wrapper for GitHub Copilot ACP."""

from __future__ import annotations

import os
import shlex
from typing import Any

from agent.external_acp_client import ExternalACPClient

ACP_MARKER_BASE_URL = "acp://copilot"


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_COPILOT_ACP_ARGS", "").strip()
    if not raw:
        return ["--acp", "--stdio"]
    return shlex.split(raw)


class CopilotACPClient(ExternalACPClient):
    """OpenAI-compatible facade for GitHub Copilot ACP."""

    def __init__(self, **kwargs: Any):
        kwargs.setdefault("api_key", "copilot-acp")
        kwargs.setdefault("base_url", ACP_MARKER_BASE_URL)
        kwargs.setdefault("command", _resolve_command())
        kwargs.setdefault("args", _resolve_args())
        super().__init__(
            provider_id="copilot-acp",
            provider_name="GitHub Copilot ACP",
            **kwargs,
        )
