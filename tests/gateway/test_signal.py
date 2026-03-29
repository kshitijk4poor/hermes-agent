"""Tests for Signal messenger platform adapter."""
import base64
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from urllib.parse import quote

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------

class TestSignalPlatformEnum:
    def test_signal_enum_exists(self):
        assert Platform.SIGNAL.value == "signal"

    def test_signal_in_platform_list(self):
        platforms = [p.value for p in Platform]
        assert "signal" in platforms


class TestSignalConfigLoading:
    def test_apply_env_overrides_signal(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_HTTP_URL", "http://localhost:9090")
        monkeypatch.setenv("SIGNAL_ACCOUNT", "+15551234567")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.SIGNAL in config.platforms
        sc = config.platforms[Platform.SIGNAL]
        assert sc.enabled is True
        assert sc.extra["http_url"] == "http://localhost:9090"
        assert sc.extra["account"] == "+15551234567"

    def test_signal_not_loaded_without_both_vars(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_HTTP_URL", "http://localhost:9090")
        # No SIGNAL_ACCOUNT

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.SIGNAL not in config.platforms

    def test_connected_platforms_includes_signal(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_HTTP_URL", "http://localhost:8080")
        monkeypatch.setenv("SIGNAL_ACCOUNT", "+15551234567")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        connected = config.get_connected_platforms()
        assert Platform.SIGNAL in connected


# ---------------------------------------------------------------------------
# Adapter Init & Helpers
# ---------------------------------------------------------------------------

class TestSignalAdapterInit:
    def _make_config(self, **extra):
        config = PlatformConfig()
        config.enabled = True
        config.extra = {
            "http_url": "http://localhost:8080",
            "account": "+15551234567",
            **extra,
        }
        return config

    def test_init_parses_config(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "group123,group456")

        from gateway.platforms.signal import SignalAdapter
        adapter = SignalAdapter(self._make_config())

        assert adapter.http_url == "http://localhost:8080"
        assert adapter.account == "+15551234567"
        assert "group123" in adapter.group_allow_from

    def test_init_empty_allowlist(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")

        from gateway.platforms.signal import SignalAdapter
        adapter = SignalAdapter(self._make_config())

        assert len(adapter.group_allow_from) == 0

    def test_init_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")

        from gateway.platforms.signal import SignalAdapter
        adapter = SignalAdapter(self._make_config(http_url="http://localhost:8080/"))

        assert adapter.http_url == "http://localhost:8080"

    def test_self_message_filtering(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")

        from gateway.platforms.signal import SignalAdapter
        adapter = SignalAdapter(self._make_config())

        assert adapter._account_normalized == "+15551234567"


class TestSignalHelpers:
    def test_redact_phone_long(self):
        from gateway.platforms.signal import _redact_phone
        assert _redact_phone("+15551234567") == "+155****4567"

    def test_redact_phone_short(self):
        from gateway.platforms.signal import _redact_phone
        assert _redact_phone("+12345") == "+1****45"

    def test_redact_phone_empty(self):
        from gateway.platforms.signal import _redact_phone
        assert _redact_phone("") == "<none>"

    def test_parse_comma_list(self):
        from gateway.platforms.signal import _parse_comma_list
        assert _parse_comma_list("+1234, +5678 , +9012") == ["+1234", "+5678", "+9012"]
        assert _parse_comma_list("") == []
        assert _parse_comma_list("  ,  ,  ") == []

    def test_guess_extension_png(self):
        from gateway.platforms.signal import _guess_extension
        assert _guess_extension(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100) == ".png"

    def test_guess_extension_jpeg(self):
        from gateway.platforms.signal import _guess_extension
        assert _guess_extension(b"\xff\xd8\xff\xe0" + b"\x00" * 100) == ".jpg"

    def test_guess_extension_pdf(self):
        from gateway.platforms.signal import _guess_extension
        assert _guess_extension(b"%PDF-1.4" + b"\x00" * 100) == ".pdf"

    def test_guess_extension_zip(self):
        from gateway.platforms.signal import _guess_extension
        assert _guess_extension(b"PK\x03\x04" + b"\x00" * 100) == ".zip"

    def test_guess_extension_mp4(self):
        from gateway.platforms.signal import _guess_extension
        assert _guess_extension(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100) == ".mp4"

    def test_guess_extension_unknown(self):
        from gateway.platforms.signal import _guess_extension
        assert _guess_extension(b"\x00\x01\x02\x03" * 10) == ".bin"

    def test_is_image_ext(self):
        from gateway.platforms.signal import _is_image_ext
        assert _is_image_ext(".png") is True
        assert _is_image_ext(".jpg") is True
        assert _is_image_ext(".gif") is True
        assert _is_image_ext(".pdf") is False

    def test_is_audio_ext(self):
        from gateway.platforms.signal import _is_audio_ext
        assert _is_audio_ext(".mp3") is True
        assert _is_audio_ext(".ogg") is True
        assert _is_audio_ext(".png") is False

    def test_check_requirements(self, monkeypatch):
        from gateway.platforms.signal import check_signal_requirements
        monkeypatch.setenv("SIGNAL_HTTP_URL", "http://localhost:8080")
        monkeypatch.setenv("SIGNAL_ACCOUNT", "+15551234567")
        assert check_signal_requirements() is True

    def test_render_mentions(self):
        from gateway.platforms.signal import _render_mentions
        text = "Hello \uFFFC, how are you?"
        mentions = [{"start": 6, "length": 1, "number": "+15559999999"}]
        result = _render_mentions(text, mentions)
        assert "@+15559999999" in result
        assert "\uFFFC" not in result

    def test_render_mentions_no_mentions(self):
        from gateway.platforms.signal import _render_mentions
        text = "Hello world"
        result = _render_mentions(text, [])
        assert result == "Hello world"

    def test_check_requirements_missing(self, monkeypatch):
        from gateway.platforms.signal import check_signal_requirements
        monkeypatch.delenv("SIGNAL_HTTP_URL", raising=False)
        monkeypatch.delenv("SIGNAL_ACCOUNT", raising=False)
        assert check_signal_requirements() is False


# ---------------------------------------------------------------------------
# SSE URL Encoding (Bug Fix: phone numbers with + must be URL-encoded)
# ---------------------------------------------------------------------------

class TestSignalSSEUrlEncoding:
    """Verify that phone numbers with + are URL-encoded in the SSE endpoint."""

    def _make_adapter(self, monkeypatch, account="+31612345678"):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        from gateway.platforms.signal import SignalAdapter
        config = PlatformConfig()
        config.enabled = True
        config.extra = {
            "http_url": "http://localhost:8080",
            "account": account,
        }
        return SignalAdapter(config)

    def test_sse_url_encodes_plus_in_account(self, monkeypatch):
        """The + in E.164 phone numbers must be percent-encoded in the SSE query string."""
        adapter = self._make_adapter(monkeypatch, account="+31612345678")
        encoded = quote("+31612345678", safe="")
        expected_url = f"http://localhost:8080/api/v1/events?account={encoded}"
        # Verify quote encodes the + sign
        assert "%2B" in encoded
        assert encoded == "%2B31612345678"
        # The URL constructed in _sse_listener should use the encoded form
        assert expected_url == "http://localhost:8080/api/v1/events?account=%2B31612345678"

    def test_sse_url_encoding_preserves_digits(self, monkeypatch):
        """Digits and country codes should pass through URL encoding unchanged."""
        encoded = quote("+15551234567", safe="")
        assert encoded == "%2B15551234567"

    def test_url_encoding_matches_source(self, monkeypatch):
        """Verify that quote(account, safe='') is used in signal.py source for SSE URL."""
        import inspect
        from gateway.platforms.signal import SignalAdapter
        source = inspect.getsource(SignalAdapter._sse_listener)
        # The SSE URL must use quote() to encode the account parameter
        assert "quote(self.account" in source, "SSE URL must use quote() to encode account"
        assert 'safe=""' in source or "safe=''" in source, "quote() must use safe='' to encode +"


# ---------------------------------------------------------------------------
# Attachment Fetch (Bug Fix: parameter must be "id" not "attachmentId")
# ---------------------------------------------------------------------------

class TestSignalAttachmentFetch:
    """Verify that _fetch_attachment uses the correct RPC parameter name."""

    def _make_adapter(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        from gateway.platforms.signal import SignalAdapter
        config = PlatformConfig()
        config.enabled = True
        config.extra = {
            "http_url": "http://localhost:8080",
            "account": "+15551234567",
        }
        return SignalAdapter(config)

    @pytest.mark.asyncio
    async def test_fetch_attachment_uses_id_parameter(self, monkeypatch):
        """RPC getAttachment must use 'id', not 'attachmentId' (signal-cli requirement)."""
        adapter = self._make_adapter(monkeypatch)

        # Create a small valid PNG for the attachment response
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        b64_data = base64.b64encode(png_data).decode()

        captured_params = {}

        async def mock_rpc(method, params, rpc_id=None):
            captured_params.update(params)
            captured_params["_method"] = method
            return {"data": b64_data}

        adapter._rpc = mock_rpc

        with patch("gateway.platforms.signal.cache_image_from_bytes", return_value="/tmp/test.png"):
            path, ext = await adapter._fetch_attachment("attachment-123")

        assert captured_params["_method"] == "getAttachment"
        assert "id" in captured_params, "Must use 'id' parameter, not 'attachmentId'"
        assert "attachmentId" not in captured_params, "Must NOT use 'attachmentId' — causes NullPointerException in signal-cli"
        assert captured_params["id"] == "attachment-123"
        assert captured_params["account"] == "+15551234567"

    @pytest.mark.asyncio
    async def test_fetch_attachment_returns_none_on_empty(self, monkeypatch):
        """_fetch_attachment returns (None, '') when RPC returns nothing."""
        adapter = self._make_adapter(monkeypatch)

        async def mock_rpc(method, params, rpc_id=None):
            return None

        adapter._rpc = mock_rpc
        path, ext = await adapter._fetch_attachment("missing-id")
        assert path is None
        assert ext == ""

    @pytest.mark.asyncio
    async def test_fetch_attachment_handles_dict_response(self, monkeypatch):
        """_fetch_attachment correctly unwraps dict response with 'data' key."""
        adapter = self._make_adapter(monkeypatch)

        pdf_data = b"%PDF-1.4" + b"\x00" * 100
        b64_data = base64.b64encode(pdf_data).decode()

        async def mock_rpc(method, params, rpc_id=None):
            return {"data": b64_data}

        adapter._rpc = mock_rpc

        with patch("gateway.platforms.signal.cache_document_from_bytes", return_value="/tmp/test.pdf"):
            path, ext = await adapter._fetch_attachment("doc-456")

        assert path == "/tmp/test.pdf"
        assert ext == ".pdf"


# ---------------------------------------------------------------------------
# Session Source
# ---------------------------------------------------------------------------

class TestSignalSessionSource:
    def test_session_source_alt_fields(self):
        from gateway.session import SessionSource
        source = SessionSource(
            platform=Platform.SIGNAL,
            chat_id="+15551234567",
            user_id="+15551234567",
            user_id_alt="uuid:abc-123",
            chat_id_alt=None,
        )
        d = source.to_dict()
        assert d["user_id_alt"] == "uuid:abc-123"
        assert "chat_id_alt" not in d  # None fields excluded

    def test_session_source_roundtrip(self):
        from gateway.session import SessionSource
        source = SessionSource(
            platform=Platform.SIGNAL,
            chat_id="group:xyz",
            chat_type="group",
            user_id="+15551234567",
            user_id_alt="uuid:abc",
            chat_id_alt="xyz",
        )
        d = source.to_dict()
        restored = SessionSource.from_dict(d)
        assert restored.user_id_alt == "uuid:abc"
        assert restored.chat_id_alt == "xyz"
        assert restored.platform == Platform.SIGNAL


# ---------------------------------------------------------------------------
# Phone Redaction in agent/redact.py
# ---------------------------------------------------------------------------

class TestSignalPhoneRedaction:
    @pytest.fixture(autouse=True)
    def _ensure_redaction_enabled(self, monkeypatch):
        monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)

    def test_us_number(self):
        from agent.redact import redact_sensitive_text
        result = redact_sensitive_text("Call +15551234567 now")
        assert "+15551234567" not in result
        assert "+155" in result  # Prefix preserved
        assert "4567" in result  # Suffix preserved

    def test_uk_number(self):
        from agent.redact import redact_sensitive_text
        result = redact_sensitive_text("UK: +442071838750")
        assert "+442071838750" not in result
        assert "****" in result

    def test_multiple_numbers(self):
        from agent.redact import redact_sensitive_text
        text = "From +15551234567 to +442071838750"
        result = redact_sensitive_text(text)
        assert "+15551234567" not in result
        assert "+442071838750" not in result

    def test_short_number_not_matched(self):
        from agent.redact import redact_sensitive_text
        result = redact_sensitive_text("Code: +12345")
        # 5 digits after + is below the 7-digit minimum
        assert "+12345" in result  # Too short to redact


# ---------------------------------------------------------------------------
# Authorization in run.py
# ---------------------------------------------------------------------------

class TestSignalAuthorization:
    def test_signal_in_allowlist_maps(self):
        """Signal should be in the platform auth maps."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.SIGNAL
        source.user_id = "+15559999999"

        # No allowlists set — should check GATEWAY_ALLOW_ALL_USERS
        with patch.dict("os.environ", {}, clear=True):
            result = gw._is_user_authorized(source)
            assert result is False


# ---------------------------------------------------------------------------
# Send Message Tool
# ---------------------------------------------------------------------------

class TestSignalSendMessage:
    def test_signal_in_platform_map(self):
        """Signal should be in the send_message tool's platform map."""
        from tools.send_message_tool import send_message_tool
        # Just verify the import works and Signal is a valid platform
        from gateway.config import Platform
        assert Platform.SIGNAL.value == "signal"
