"""Provider-specific wrapper for Cursor ACP."""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Any

from agent.external_acp_client import ExternalACPClient

ACP_MARKER_BASE_URL = "acp://cursor"


def _resolve_command() -> str:
    explicit = (
        os.getenv("HERMES_CURSOR_ACP_COMMAND", "").strip()
        or os.getenv("CURSOR_AGENT_PATH", "").strip()
    )
    if explicit:
        return explicit
    for candidate in ("cursor-agent", "agent"):
        if shutil.which(candidate):
            return candidate
    return "cursor-agent"


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_CURSOR_ACP_ARGS", "").strip()
    if not raw:
        return ["acp"]
    return shlex.split(raw)


class CursorACPClient(ExternalACPClient):
    """OpenAI-compatible facade for Cursor ACP."""

    def __init__(self, **kwargs: Any):
        kwargs.setdefault("api_key", "cursor-acp")
        kwargs.setdefault("base_url", ACP_MARKER_BASE_URL)
        kwargs.setdefault("command", _resolve_command())
        kwargs.setdefault("args", _resolve_args())
        super().__init__(
            provider_id="cursor-acp",
            provider_name="Cursor ACP",
            **kwargs,
        )
