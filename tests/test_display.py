"""Tests for agent/display.py — build_tool_preview()."""

import pytest
from unittest.mock import MagicMock, mock_open, patch

from agent.display import (
    build_tool_preview,
    extract_edit_diff,
    render_edit_diff_with_hunk,
)


class TestBuildToolPreview:
    """Tests for build_tool_preview defensive handling and normal operation."""

    def test_none_args_returns_none(self):
        """PR #453: None args should not crash, should return None."""
        assert build_tool_preview("terminal", None) is None

    def test_empty_dict_returns_none(self):
        """Empty dict has no keys to preview."""
        assert build_tool_preview("terminal", {}) is None

    def test_known_tool_with_primary_arg(self):
        """Known tool with its primary arg should return a preview string."""
        result = build_tool_preview("terminal", {"command": "ls -la"})
        assert result is not None
        assert "ls -la" in result

    def test_web_search_preview(self):
        result = build_tool_preview("web_search", {"query": "hello world"})
        assert result is not None
        assert "hello world" in result

    def test_read_file_preview(self):
        result = build_tool_preview("read_file", {"path": "/tmp/test.py", "offset": 1})
        assert result is not None
        assert "/tmp/test.py" in result

    def test_unknown_tool_with_fallback_key(self):
        """Unknown tool but with a recognized fallback key should still preview."""
        result = build_tool_preview("custom_tool", {"query": "test query"})
        assert result is not None
        assert "test query" in result

    def test_unknown_tool_no_matching_key(self):
        """Unknown tool with no recognized keys should return None."""
        result = build_tool_preview("custom_tool", {"foo": "bar"})
        assert result is None

    def test_long_value_truncated(self):
        """Preview should truncate long values."""
        long_cmd = "a" * 100
        result = build_tool_preview("terminal", {"command": long_cmd}, max_len=40)
        assert result is not None
        assert len(result) <= 43  # max_len + "..."

    def test_process_tool_with_none_args(self):
        """Process tool special case should also handle None args."""
        assert build_tool_preview("process", None) is None

    def test_process_tool_normal(self):
        result = build_tool_preview("process", {"action": "poll", "session_id": "abc123"})
        assert result is not None
        assert "poll" in result

    def test_todo_tool_read(self):
        result = build_tool_preview("todo", {"merge": False})
        assert result is not None
        assert "reading" in result

    def test_todo_tool_with_todos(self):
        result = build_tool_preview("todo", {"todos": [{"id": "1", "content": "test", "status": "pending"}]})
        assert result is not None
        assert "1 task" in result

    def test_memory_tool_add(self):
        result = build_tool_preview("memory", {"action": "add", "target": "user", "content": "test note"})
        assert result is not None
        assert "user" in result

    def test_session_search_preview(self):
        result = build_tool_preview("session_search", {"query": "find something"})
        assert result is not None
        assert "find something" in result

    def test_false_like_args_zero(self):
        """Non-dict falsy values should return None, not crash."""
        assert build_tool_preview("terminal", 0) is None
        assert build_tool_preview("terminal", "") is None
        assert build_tool_preview("terminal", []) is None


class _FakeTTY:
    def isatty(self):
        return True


class TestEditDiffPreview:
    def test_extract_edit_diff_for_patch(self):
        diff = extract_edit_diff("patch", '{"success": true, "diff": "--- a/x\\n+++ b/x\\n"}')
        assert diff is not None
        assert "+++ b/x" in diff

    def test_extract_edit_diff_ignores_non_edit_tools(self):
        assert extract_edit_diff("read_file", '{"diff": "--- a\\n+++ b\\n"}') is None

    def test_render_edit_diff_with_hunk_invokes_pager(self, monkeypatch):
        fake_run = MagicMock(return_value=MagicMock(returncode=0))
        printer = MagicMock()

        monkeypatch.setattr("agent.display._resolve_hunk_command", lambda: ["hunk"])
        monkeypatch.setattr("agent.display.sys.stdin", _FakeTTY())
        monkeypatch.setattr("agent.display.sys.stdout", _FakeTTY())

        with patch("agent.display.subprocess.run", fake_run), patch("builtins.open", mock_open()) as mocked_open:
            rendered = render_edit_diff_with_hunk(
                "write_file",
                '{"diff": "--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n"}',
                print_fn=printer,
            )

        assert rendered is True
        printer.assert_called_once()
        mocked_open.assert_any_call("/dev/tty", "rb", buffering=0)
        args = fake_run.call_args.args[0]
        assert args[:2] == ["hunk", "patch"]
        assert args[-1] == "--pager"

    def test_render_edit_diff_with_hunk_skips_without_tty(self, monkeypatch):
        fake_run = MagicMock()
        fake_stream = MagicMock()
        fake_stream.isatty.return_value = False

        monkeypatch.setattr("agent.display.sys.stdin", fake_stream)
        monkeypatch.setattr("agent.display.sys.stdout", fake_stream)
        monkeypatch.setattr("agent.display._resolve_hunk_command", lambda: ["hunk"])

        with patch("agent.display.subprocess.run", fake_run):
            rendered = render_edit_diff_with_hunk(
                "patch",
                '{"diff": "--- a/x\\n+++ b/x\\n"}',
            )

        assert rendered is False
        fake_run.assert_not_called()
