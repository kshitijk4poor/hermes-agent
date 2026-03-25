"""OpenAI-compatible shim that forwards Hermes requests to ACP agents."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

_DEFAULT_TIMEOUT_SECONDS = 900.0


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if "content" in content and isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    *,
    provider_name: str,
    model: str | None = None,
) -> str:
    sections: list[str] = [
        f"You are being used as the active {provider_name} ACP agent backend for Hermes.",
        "Use your own ACP capabilities and respond directly in natural language.",
        "Do not emit OpenAI tool-call JSON.",
    ]
    if model:
        sections.append(f"Hermes requested model hint: {model}")

    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").strip().lower()
        if role == "tool":
            role = "tool"
        elif role not in {"system", "user", "assistant"}:
            role = "context"

        content = message.get("content")
        rendered = _render_message_content(content)
        if not rendered:
            continue

        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
            "context": "Context",
        }.get(role, role.title())
        transcript.append(f"{label}:\n{rendered}")

    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))

    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _acp_estimate_usage_enabled() -> bool:
    """Approximate tokens (chars/4) when ACP omits usage.

    Default on so UIs (e.g. Cursor composer context bar) get non-zero PromptResponse.usage.
    Set HERMES_ACP_ESTIMATE_USAGE=0/false/off/no to disable.
    """
    raw = os.getenv("HERMES_ACP_ESTIMATE_USAGE", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return True


def _finalize_acp_usage(
    prompt_text: str,
    response_text: str,
    reasoning_text: str,
    usage_from_result: dict[str, int] | None,
) -> dict[str, int] | None:
    """Prefer real usage from ACP; optionally fill with a char/4 heuristic (not billing-grade)."""
    if usage_from_result and any(
        int(usage_from_result.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        return usage_from_result
    if not _acp_estimate_usage_enabled():
        return usage_from_result
    pi = max(0, len(prompt_text) // 4)
    ci = max(0, (len(response_text) + len(reasoning_text)) // 4)
    return {"prompt_tokens": pi, "completion_tokens": ci, "total_tokens": pi + ci}


def _usage_from_acp_prompt_result(result: Any) -> dict[str, int] | None:
    """If the ACP `session/prompt` result includes usage, map it for OpenAI-style clients."""
    if not isinstance(result, dict):
        return None
    raw = result.get("usage")
    data: dict[str, Any] = raw if isinstance(raw, dict) else result
    prompt = data.get("prompt_tokens")
    if prompt is None:
        prompt = data.get("input_tokens")
    completion = data.get("completion_tokens")
    if completion is None:
        completion = data.get("output_tokens")
    total = data.get("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    pi = int(prompt or 0)
    ci = int(completion or 0)
    ti = int(total if total is not None else pi + ci)
    return {"prompt_tokens": pi, "completion_tokens": ci, "total_tokens": ti}


def _ensure_path_within_cwd(path_text: str, cwd: str) -> Path:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved = candidate.resolve()
    root = Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside the session cwd '{root}'.") from exc
    return resolved


class _ACPChatCompletions:
    def __init__(self, client: "ExternalACPClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ACPChatNamespace:
    def __init__(self, client: "ExternalACPClient"):
        self.completions = _ACPChatCompletions(client)


class ExternalACPClient:
    """Minimal OpenAI-client-compatible facade for ACP-backed providers."""

    def __init__(
        self,
        *,
        provider_id: str,
        provider_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **_: Any,
    ):
        self._provider_id = provider_id
        self._provider_name = provider_name
        self.api_key = api_key or provider_id
        self.base_url = base_url or f"acp://{provider_id}"
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or ""
        self._acp_args = list(acp_args or args or [])
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = _ACPChatNamespace(self)
        self.is_closed = False
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_lock = threading.Lock()

    def close(self) -> None:
        proc: subprocess.Popen[str] | None
        with self._active_process_lock:
            proc = self._active_process
            self._active_process = None
        self.is_closed = True
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        **extra: Any,
    ) -> Any:
        on_text_delta = extra.pop("_hermes_acp_text_delta", None)
        on_reasoning_delta = extra.pop("_hermes_acp_reasoning_delta", None)
        prompt_text = _format_messages_as_prompt(
            messages or [],
            provider_name=self._provider_name,
            model=model,
        )
        response_text, reasoning_text, usage_nums = self._run_prompt(
            prompt_text,
            timeout_seconds=float(timeout or _DEFAULT_TIMEOUT_SECONDS),
            on_text_delta=on_text_delta if callable(on_text_delta) else None,
            on_reasoning_delta=on_reasoning_delta if callable(on_reasoning_delta) else None,
        )
        usage_nums = _finalize_acp_usage(prompt_text, response_text, reasoning_text, usage_nums)

        pt = usage_nums.get("prompt_tokens", 0) if usage_nums else 0
        ct = usage_nums.get("completion_tokens", 0) if usage_nums else 0
        tt = usage_nums.get("total_tokens", 0) if usage_nums else 0
        usage = SimpleNamespace(
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        assistant_message = SimpleNamespace(
            content=response_text,
            tool_calls=[],
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        choice = SimpleNamespace(message=assistant_message, finish_reason="stop")
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or self._provider_id,
        )

    def _run_prompt(
        self,
        prompt_text: str,
        *,
        timeout_seconds: float,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> tuple[str, str, dict[str, int] | None]:
        try:
            proc = subprocess.Popen(
                [self._acp_command] + self._acp_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self._acp_cwd,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start {self._provider_name} command '{self._acp_command}'."
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError(f"{self._provider_name} process did not expose stdin/stdout pipes.")

        self.is_closed = False
        with self._active_process_lock:
            self._active_process = proc

        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=40)

        def _stdout_reader() -> None:
            for line in proc.stdout:
                try:
                    inbox.put(json.loads(line))
                except Exception:
                    inbox.put({"raw": line.rstrip("\n")})

        def _stderr_reader() -> None:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                stderr_tail.append(line.rstrip("\n"))

        out_thread = threading.Thread(target=_stdout_reader, daemon=True)
        err_thread = threading.Thread(target=_stderr_reader, daemon=True)
        out_thread.start()
        err_thread.start()

        next_id = 0

        def _request(
            method: str,
            params: dict[str, Any],
            *,
            text_parts: list[str] | None = None,
            reasoning_parts: list[str] | None = None,
            text_delta_cb: Callable[[str], None] | None = None,
            reasoning_delta_cb: Callable[[str], None] | None = None,
        ) -> Any:
            nonlocal next_id
            next_id += 1
            request_id = next_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue

                if self._handle_server_message(
                    msg,
                    process=proc,
                    cwd=self._acp_cwd,
                    text_parts=text_parts,
                    reasoning_parts=reasoning_parts,
                    on_text_delta=text_delta_cb,
                    on_reasoning_delta=reasoning_delta_cb,
                ):
                    continue

                if msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise RuntimeError(
                        f"{self._provider_name} {method} failed: {err.get('message') or err}"
                    )
                return msg.get("result")

            stderr_text = "\n".join(stderr_tail).strip()
            if proc.poll() is not None and stderr_text:
                raise RuntimeError(f"{self._provider_name} process exited early: {stderr_text}")
            raise TimeoutError(f"Timed out waiting for {self._provider_name} response to {method}.")

        try:
            _request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": True,
                            "writeTextFile": True,
                        }
                    },
                    "clientInfo": {
                        "name": "hermes-agent",
                        "title": "Hermes Agent",
                        "version": "0.0.0",
                    },
                },
            )
            session = _request(
                "session/new",
                {
                    "cwd": self._acp_cwd,
                    "mcpServers": [],
                },
            ) or {}
            session_id = str(session.get("sessionId") or "").strip()
            if not session_id:
                raise RuntimeError(f"{self._provider_name} did not return a sessionId.")

            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            prompt_result = _request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [
                        {
                            "type": "text",
                            "text": prompt_text,
                        }
                    ],
                },
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                text_delta_cb=on_text_delta,
                reasoning_delta_cb=on_reasoning_delta,
            )
            usage_nums = _usage_from_acp_prompt_result(prompt_result)
            return "".join(text_parts), "".join(reasoning_parts), usage_nums
        finally:
            self.close()

    def _handle_server_message(
        self,
        msg: dict[str, Any],
        *,
        process: subprocess.Popen[str],
        cwd: str,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> bool:
        method = msg.get("method")
        if not isinstance(method, str):
            return False

        if method == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            kind = str(update.get("sessionUpdate") or "").strip()
            content = update.get("content") or {}
            chunk_text = ""
            if isinstance(content, dict):
                chunk_text = str(content.get("text") or "")
            if kind == "agent_message_chunk" and chunk_text and text_parts is not None:
                text_parts.append(chunk_text)
                if on_text_delta:
                    try:
                        on_text_delta(chunk_text)
                    except Exception:
                        pass
            elif kind == "agent_thought_chunk" and chunk_text and reasoning_parts is not None:
                reasoning_parts.append(chunk_text)
                if on_reasoning_delta:
                    try:
                        on_reasoning_delta(chunk_text)
                    except Exception:
                        pass
            return True

        if process.stdin is None:
            return True

        message_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "session/request_permission":
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "outcome": {
                        "outcome": "allow_once",
                    }
                },
            }
        elif method == "fs/read_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                content = path.read_text() if path.exists() else ""
                line = params.get("line")
                limit = params.get("limit")
                if isinstance(line, int) and line > 1:
                    lines = content.splitlines(keepends=True)
                    start = line - 1
                    end = start + limit if isinstance(limit, int) and limit > 0 else None
                    content = "".join(lines[start:end])
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": content,
                    },
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        elif method == "fs/write_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content") or ""))
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": None,
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        else:
            response = _jsonrpc_error(
                message_id,
                -32601,
                f"ACP client method '{method}' is not supported by Hermes yet.",
            )

        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True
