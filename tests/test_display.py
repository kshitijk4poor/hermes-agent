"""Tests for agent/display.py — build_tool_preview()."""

import pytest
from unittest.mock import MagicMock, patch

from agent.display import (
    build_tool_preview,
    capture_local_edit_snapshot,
    extract_edit_diff,
    render_edit_diff_with_delta,
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


class TestEditDiffPreview:
    def test_extract_edit_diff_for_patch(self):
        diff = extract_edit_diff("patch", '{"success": true, "diff": "--- a/x\\n+++ b/x\\n"}')
        assert diff is not None
        assert "+++ b/x" in diff

    def test_extract_edit_diff_ignores_non_edit_tools(self):
        assert extract_edit_diff("write_file", '{"diff": "--- a\\n+++ b\\n"}') is None

    def test_extract_edit_diff_uses_local_snapshot_for_write_file(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("old\\n", encoding="utf-8")

        snapshot = capture_local_edit_snapshot("write_file", {"path": str(target)})

        target.write_text("new\\n", encoding="utf-8")

        diff = extract_edit_diff(
            "write_file",
            '{"bytes_written": 4}',
            function_args={"path": str(target)},
            snapshot=snapshot,
        )

        assert diff is not None
        assert "--- a/" in diff
        assert "+++ b/" in diff
        assert "-old" in diff
        assert "+new" in diff

    def test_extract_edit_diff_uses_local_snapshot_for_skill_manage_patch(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "teknium-dev"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("---\\nname: test\\n---\\nold\\n", encoding="utf-8")

        monkeypatch.setattr(
            "tools.skill_manager_tool._find_skill",
            lambda name: {"path": skill_dir} if name == "teknium-dev" else None,
        )

        args = {"action": "patch", "name": "teknium-dev"}
        snapshot = capture_local_edit_snapshot("skill_manage", args)

        skill_file.write_text("---\\nname: test\\n---\\nnew\\n", encoding="utf-8")

        diff = extract_edit_diff(
            "skill_manage",
            '{"success": true, "message": "patched"}',
            function_args=args,
            snapshot=snapshot,
        )

        assert diff is not None
        assert "SKILL.md" in diff
        assert "old" in diff
        assert "new" in diff

    def test_extract_edit_diff_uses_local_snapshot_for_skill_manage_delete(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "teknium-dev"
        references_dir = skill_dir / "references"
        references_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\\nname: test\\n---\\nbody\\n", encoding="utf-8")
        (references_dir / "notes.md").write_text("notes\\n", encoding="utf-8")

        monkeypatch.setattr(
            "tools.skill_manager_tool._find_skill",
            lambda name: {"path": skill_dir} if name == "teknium-dev" else None,
        )

        args = {"action": "delete", "name": "teknium-dev"}
        snapshot = capture_local_edit_snapshot("skill_manage", args)

        for path in sorted(skill_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()

        diff = extract_edit_diff(
            "skill_manage",
            '{"success": true, "message": "deleted"}',
            function_args=args,
            snapshot=snapshot,
        )

        assert diff is not None
        assert "SKILL.md" in diff
        assert "references/notes.md" in diff

    def test_render_edit_diff_with_delta_invokes_pager(self, monkeypatch):
        fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="\x1b[32m+new\x1b[0m\n", stderr=""))
        printer = MagicMock()

        monkeypatch.setattr("tools.delta_bootstrap.resolve_delta_command", lambda: ["delta", "--paging=always"])

        with patch("agent.display.subprocess.run", fake_run):
            rendered = render_edit_diff_with_delta(
                "patch",
                '{"diff": "--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n"}',
                print_fn=printer,
            )

        assert rendered is True
        assert printer.call_count >= 2
        args = fake_run.call_args.args[0]
        assert args == ["delta", "--paging=never"]

    def test_render_edit_diff_with_delta_skips_without_diff(self, monkeypatch):
        fake_run = MagicMock()
        monkeypatch.setattr("tools.delta_bootstrap.resolve_delta_command", lambda: ["delta", "--paging=always"])

        with patch("agent.display.subprocess.run", fake_run):
            rendered = render_edit_diff_with_delta(
                "patch",
                '{"success": true}',
            )

        assert rendered is False
        fake_run.assert_not_called()

    def test_render_edit_diff_with_delta_falls_back_to_plain_diff_when_missing(self, monkeypatch):
        printer = MagicMock()

        monkeypatch.setattr("tools.delta_bootstrap.resolve_delta_command", lambda: None)

        rendered = render_edit_diff_with_delta(
            "patch",
            '{"diff": "--- a/x\\n+++ b/x\\n"}',
            print_fn=printer,
        )

        assert rendered is True
        assert printer.call_count >= 2

    def test_render_edit_diff_with_delta_falls_back_to_plain_diff_on_error(self, monkeypatch):
        fake_run = MagicMock(side_effect=OSError("boom"))
        printer = MagicMock()

        monkeypatch.setattr("tools.delta_bootstrap.resolve_delta_command", lambda: ["delta", "--paging=always"])

        with patch("agent.display.subprocess.run", fake_run):
            rendered = render_edit_diff_with_delta(
                "patch",
                '{"diff": "--- a/x\\n+++ b/x\\n"}',
                print_fn=printer,
            )

        assert rendered is True
        assert printer.call_count >= 2
