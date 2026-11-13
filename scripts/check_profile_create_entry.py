#!/usr/bin/env python3
"""Fail when anything but ``hermes_cli/profile_provision.py`` calls ``create_profile``.

``provision_profile(ProfileCreateRequest)`` is the one way to create a profile: the CLI, the dashboard
REST route and the Desktop/TUI RPC all build a request and render the result. Before it existed each
surface called ``hermes_cli.profiles.create_profile`` directly and added its own post-create steps, so the
same "new profile" action produced different profiles (credentials, skills, model) depending on where it
started. A new direct caller would reopen that split.

Flags a call whose callee is ``create_profile`` (bare or as an attribute) in the scanned trees, except in
``hermes_cli/profiles.py`` (its definition) and ``hermes_cli/profile_provision.py`` (the entry point).
Tests are not scanned. Suppress a true exception with ``# profile-create: ok — <why>`` on the call's line.

Usage: python3 scripts/check_profile_create_entry.py [paths...]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TREES = ("hermes_cli", "agent", "gateway", "tui_gateway", "cron", "plugins", "tools", "cli.py")
ALLOWED_FILES = {ROOT / "hermes_cli" / "profiles.py", ROOT / "hermes_cli" / "profile_provision.py"}
SUPPRESS = "# profile-create: ok"


def scan_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []
    lines = src.splitlines()
    rel = path.relative_to(ROOT).as_posix()
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name == "create_profile" and SUPPRESS not in lines[node.lineno - 1]:
            problems.append(f"{rel}:{node.lineno}: direct create_profile() call")
    return problems


def main(argv: list[str]) -> int:
    targets = [ROOT / a for a in argv] or [ROOT / t for t in DEFAULT_TREES]
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files.extend(p for p in t.rglob("*.py") if "node_modules" not in p.parts)
        elif t.suffix == ".py" and t.exists():
            files.append(t)
    problems: list[str] = []
    for f in sorted(set(files)):
        if f.resolve() not in ALLOWED_FILES:
            problems.extend(scan_file(f))
    if problems:
        print("Create profiles through hermes_cli.profile_provision.provision_profile:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"check_profile_create_entry: OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
