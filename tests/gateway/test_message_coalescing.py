"""Tests for gateway inbound message coalescing."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import (
    GatewayConfig,
    MessageCoalescingConfig,
    Platform,
    PlatformConfig,
    load_gateway_config,
)
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class DummyCoalescingAdapter(BasePlatformAdapter):
    """Minimal adapter for platform-agnostic coalescing tests."""

    def __init__(self, *, extra=None):
        super().__init__(
            PlatformConfig(enabled=True, token="fake-token", extra=extra or {}),
            Platform.TELEGRAM,
        )

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _make_event(
    text: str,
    *,
    chat_type: str = "group",
    chat_id: str = "12345",
    user_id: str = "u-1",
    user_name: str = "Alice",
    message_id: str = "1",
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        ),
        message_id=message_id,
    )


class TestMessageCoalescingConfig:
    def test_roundtrip_preserves_message_coalescing_settings(self):
        config = GatewayConfig(
            message_coalescing=MessageCoalescingConfig(
                enabled=True,
                debounce_ms=1500,
                max_wait_ms=5000,
                min_messages=2,
                multi_user_only=True,
                include_hint=True,
            )
        )

        restored = GatewayConfig.from_dict(config.to_dict())

        assert restored.message_coalescing.enabled is True
        assert restored.message_coalescing.debounce_ms == 1500
        assert restored.message_coalescing.max_wait_ms == 5000
        assert restored.message_coalescing.min_messages == 2
        assert restored.message_coalescing.multi_user_only is True
        assert restored.message_coalescing.include_hint is True

    def test_bridges_message_coalescing_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "message_coalescing:\n"
            "  enabled: true\n"
            "  debounce_ms: 1200\n"
            "  max_wait_ms: 4200\n"
            "  min_messages: 3\n"
            "  multi_user_only: false\n"
            "  include_hint: false\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.message_coalescing.enabled is True
        assert config.message_coalescing.debounce_ms == 1200
        assert config.message_coalescing.max_wait_ms == 4200
        assert config.message_coalescing.min_messages == 3
        assert config.message_coalescing.multi_user_only is False
        assert config.message_coalescing.include_hint is False

    def test_create_adapter_injects_resolved_message_coalescing(self):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            message_coalescing=MessageCoalescingConfig(
                enabled=True,
                debounce_ms=1500,
                max_wait_ms=5000,
                min_messages=2,
                multi_user_only=True,
                include_hint=True,
            ),
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    token="fake-token",
                    extra={"message_coalescing": {"debounce_ms": "oops"}},
                )
            },
        )

        adapter_config = runner.config.platforms[Platform.TELEGRAM]

        with patch("gateway.platforms.telegram.check_telegram_requirements", return_value=True), patch(
            "gateway.platforms.telegram.TelegramAdapter", side_effect=lambda cfg: cfg
        ):
            created = runner._create_adapter(Platform.TELEGRAM, adapter_config)

        assert created.extra["message_coalescing"]["debounce_ms"] == 1500
        assert created.extra["message_coalescing"]["max_wait_ms"] == 5000


class TestAdapterMessageCoalescing:
    @pytest.mark.asyncio
    async def test_group_messages_are_coalesced_into_single_dispatch(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 50,
                    "max_wait_ms": 200,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("part one", message_id="1"))
        await asyncio.sleep(0.01)
        await adapter.handle_message(_make_event("part two", message_id="2"))
        await asyncio.sleep(0.08)

        adapter._handle_message_now.assert_awaited_once()
        dispatched_event, dispatched_key = adapter._handle_message_now.call_args.args
        assert "part one" in dispatched_event.text
        assert "part two" in dispatched_event.text
        assert "respond to the overall request" in dispatched_event.text.lower()
        assert dispatched_event.message_id == "2"
        assert dispatched_key == build_session_key(dispatched_event.source)
        assert adapter._pending_coalesced_messages == {}

    @pytest.mark.asyncio
    async def test_dms_bypass_multi_user_only_coalescing(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 5000,
                    "max_wait_ms": 5000,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        event = _make_event("hello", chat_type="dm")
        await adapter.handle_message(event)

        adapter._handle_message_now.assert_awaited_once_with(
            event,
            build_session_key(event.source),
        )
        assert adapter._pending_coalesced_messages == {}

    @pytest.mark.asyncio
    async def test_command_clears_pending_coalesced_batch(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 5000,
                    "max_wait_ms": 5000,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        queued_event = _make_event("queued prompt", message_id="1")
        session_key = build_session_key(queued_event.source)
        await adapter.handle_message(queued_event)

        assert session_key in adapter._pending_coalesced_messages

        command = MessageEvent(
            text="/reset",
            message_type=MessageType.COMMAND,
            source=queued_event.source,
            message_id="cmd-1",
        )
        await adapter.handle_message(command)

        assert session_key not in adapter._pending_coalesced_messages
        assert session_key not in adapter._pending_coalescing_tasks
        adapter._handle_message_now.assert_awaited_once_with(command, session_key)

    @pytest.mark.asyncio
    async def test_max_wait_caps_repeated_timer_extensions(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 80,
                    "max_wait_ms": 120,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("one", message_id="1"))
        await asyncio.sleep(0.05)
        await adapter.handle_message(_make_event("two", message_id="2"))
        await asyncio.sleep(0.05)
        await adapter.handle_message(_make_event("three", message_id="3"))
        await asyncio.sleep(0.05)

        adapter._handle_message_now.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_batches_below_min_messages_flush_before_full_debounce(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 2000,
                    "max_wait_ms": 5000,
                    "min_messages": 3,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("single message", message_id="1"))
        await asyncio.sleep(0.45)

        adapter._handle_message_now.assert_awaited_once()
        flushed_event = adapter._handle_message_now.call_args.args[0]
        assert flushed_event.text == "single message"

    @pytest.mark.asyncio
    async def test_non_text_event_flushes_pending_batch_before_dispatch(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 5000,
                    "max_wait_ms": 5000,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        text_event = _make_event("draft text", message_id="1")
        await adapter.handle_message(text_event)

        photo_event = MessageEvent(
            text="caption",
            message_type=MessageType.PHOTO,
            source=text_event.source,
            message_id="2",
            media_urls=["/tmp/photo.jpg"],
            media_types=["image/jpeg"],
        )
        await adapter.handle_message(photo_event)

        assert adapter._handle_message_now.await_count == 2
        flushed_event = adapter._handle_message_now.await_args_list[0].args[0]
        dispatched_photo = adapter._handle_message_now.await_args_list[1].args[0]
        assert "draft text" in flushed_event.text
        assert dispatched_photo is photo_event

    @pytest.mark.asyncio
    async def test_active_session_batches_followup_texts_until_current_run_finishes(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 50,
                    "max_wait_ms": 200,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        release = asyncio.Event()
        seen_texts = []

        async def handler(event):
            seen_texts.append(event.text)
            if len(seen_texts) == 1:
                await release.wait()
            return None

        adapter.set_message_handler(handler)

        first_event = _make_event("first", message_id="1")
        await adapter._handle_message_now(first_event, build_session_key(first_event.source))
        await asyncio.sleep(0)
        await adapter.handle_message(_make_event("second", message_id="2"))
        await adapter.handle_message(_make_event("third", message_id="3"))

        release.set()
        await asyncio.sleep(0.1)

        assert seen_texts[0] == "first"
        assert len(seen_texts) == 2
        assert "second" in seen_texts[1]
        assert "third" in seen_texts[1]

    @pytest.mark.asyncio
    async def test_cancel_background_tasks_clears_pending_coalescing(self):
        adapter = DummyCoalescingAdapter(
            extra={
                "message_coalescing": {
                    "enabled": True,
                    "debounce_ms": 1000,
                    "max_wait_ms": 5000,
                    "min_messages": 2,
                    "multi_user_only": True,
                    "include_hint": True,
                }
            }
        )
        adapter.set_message_handler(AsyncMock(return_value=None))
        adapter._handle_message_now = AsyncMock()

        await adapter.handle_message(_make_event("hello"))
        assert adapter._pending_coalesced_messages
        assert adapter._pending_coalescing_tasks

        await adapter.cancel_background_tasks()

        assert adapter._pending_coalesced_messages == {}
        assert adapter._pending_coalescing_tasks == {}
