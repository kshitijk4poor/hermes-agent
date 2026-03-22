"""Tests for gateway inbound message debouncing."""

import asyncio
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class DummyDebounceAdapter(BasePlatformAdapter):
    def __init__(self, debounce_ms=0):
        super().__init__(
            PlatformConfig(enabled=True, token="fake-token", extra={"debounce_ms": debounce_ms}),
            Platform.TELEGRAM,
        )

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _make_event(text: str, *, chat_id: str = "12345", message_id: str = "1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"),
        message_id=message_id,
    )


class TestInboundDebounce:
    @pytest.mark.asyncio
    async def test_rapid_messages_are_coalesced_into_single_dispatch(self):
        adapter = DummyDebounceAdapter(debounce_ms=50)
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("part one", message_id="1"))
        await asyncio.sleep(0.01)
        await adapter.handle_message(_make_event("part two", message_id="2"))
        await asyncio.sleep(0.08)

        adapter._handle_message_now.assert_called_once()
        dispatched_event, dispatched_key = adapter._handle_message_now.call_args.args
        assert "part one" in dispatched_event.text
        assert "part two" in dispatched_event.text
        assert dispatched_event.message_id == "2"
        assert dispatched_key == build_session_key(dispatched_event.source)

    @pytest.mark.asyncio
    async def test_commands_bypass_debounce(self):
        adapter = DummyDebounceAdapter(debounce_ms=5000)
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(
            MessageEvent(
                text="/status",
                message_type=MessageType.COMMAND,
                source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
                message_id="cmd-1",
            )
        )

        adapter._handle_message_now.assert_awaited_once()
        assert adapter._pending_debounced_messages == {}

    @pytest.mark.asyncio
    async def test_command_clears_queued_debounced_message(self):
        adapter = DummyDebounceAdapter(debounce_ms=5000)
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("queued prompt", message_id="1"))
        session_key = build_session_key(_make_event("queued prompt").source)
        assert session_key in adapter._pending_debounced_messages

        await adapter.handle_message(
            MessageEvent(
                text="/reset",
                message_type=MessageType.COMMAND,
                source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
                message_id="cmd-1",
            )
        )

        assert session_key not in adapter._pending_debounced_messages
        assert session_key not in adapter._pending_debounce_tasks
        adapter._handle_message_now.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_identical_messages_are_preserved(self):
        adapter = DummyDebounceAdapter(debounce_ms=50)
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("ok", message_id="1"))
        await asyncio.sleep(0.01)
        await adapter.handle_message(_make_event("ok", message_id="2"))
        await asyncio.sleep(0.08)

        dispatched_event = adapter._handle_message_now.call_args.args[0]
        assert dispatched_event.text == "ok\n\nok"

    @pytest.mark.asyncio
    async def test_cancel_background_tasks_clears_pending_debounce(self):
        adapter = DummyDebounceAdapter(debounce_ms=1000)
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("hello"))
        assert adapter._pending_debounced_messages
        assert adapter._pending_debounce_tasks

        await adapter.cancel_background_tasks()

        assert adapter._pending_debounced_messages == {}
        assert adapter._pending_debounce_tasks == {}


class TestAdapterDebounceConfig:
    def test_create_adapter_overrides_invalid_platform_debounce_with_resolved_fallback(self):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            debounce_ms=5000,
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token", extra={"debounce_ms": "nope"})},
        )

        adapter_config = runner.config.platforms[Platform.TELEGRAM]

        with patch("gateway.platforms.telegram.check_telegram_requirements", return_value=True), \
             patch("gateway.platforms.telegram.TelegramAdapter", side_effect=lambda cfg: cfg):
            created = runner._create_adapter(Platform.TELEGRAM, adapter_config)

        assert created.extra["debounce_ms"] == 5000
