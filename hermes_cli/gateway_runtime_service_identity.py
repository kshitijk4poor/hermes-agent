"""Read-only binding checks; unknown supervisor syntax is not profile absence.

These checks recognize the installed Hermes launch formats, not arbitrary shell
programs. Manager-loaded values take precedence over filenames and disk copies.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
from typing import NoReturn


SYSTEMD_IDENTITY_PROPERTIES = (
    "User", "DynamicUser", "ExecStart", "Environment", "EnvironmentFiles",
    "PassEnvironment", "UnsetEnvironment", "PAMName", "RootDirectory", "RootImage",
    "ExecStartPre", "ExecCondition", "DropInPaths",
)


def _unverified() -> NoReturn:
    raise ValueError("service_identity_unverified")


def _absolute(value: str) -> Path:
    if not value or not Path(value).is_absolute():
        _unverified()
    return Path(value).resolve()


def verify_home(home: Path, configured: str) -> None:
    if os.path.normcase(str(_absolute(configured))) != os.path.normcase(str(home.resolve())):
        raise ValueError("profile_mismatch")


def verify_gateway_argv(argv: list[str], home: Path) -> None:
    from gateway.status import looks_like_gateway_command_line

    # The installer wraps launchd stderr with this fixed, non-shell module.
    if len(argv) > 7 and argv[1:4] == ["-m", "hermes_cli.stderr_timestamp", "--error-log"] and argv[5] == "--":
        argv = argv[6:]
    if not argv or not looks_like_gateway_command_line(subprocess_command(argv)):
        _unverified()
    executable = Path(argv[0]).name.lower()
    if re.fullmatch(r"python(?:w|\d+(?:\.\d+)*)?(?:\.exe)?", executable):
        if argv[1:3] != ["-m", "hermes_cli.main"]:
            _unverified()
        args = argv[3:]
    elif executable in {"hermes", "hermes.exe"}:
        args = argv[1:]
    else:
        _unverified()
    profiles = []
    filtered = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--profile", "-p"}:
            index += 1
            if index == len(args):
                _unverified()
            profiles.append(args[index])
        elif token.startswith("--profile="):
            profiles.append(token.split("=", 1)[1])
        else:
            filtered.append(token)
        index += 1
    if len(profiles) > 1:
        _unverified()
    if profiles:
        # An explicit selector must agree with the pinned environment. This
        # preserves custom-root profiles without borrowing the caller's root.
        expected = home.name if home.parent.name == "profiles" else "default"
        if profiles[0] != expected:
            raise ValueError("profile_mismatch")
    if filtered[:2] != ["gateway", "run"] or any(
        arg not in {"--quiet", "--external-supervisor"} for arg in filtered[2:]
    ):
        _unverified()


def subprocess_command(argv: list[str]) -> str:
    # The canonical matcher is quote-aware on both hosts.
    import subprocess
    return subprocess.list2cmdline(argv)


def environment_pairs(value: str) -> dict[str, str]:
    # systemctl uses C escapes for unusual bytes. Do not silently reinterpret
    # those with shell escaping (not the same grammar).
    if "\\" in value:
        _unverified()
    result = {}
    for item in shlex.split(value):
        key, separator, val = item.partition("=")
        if not separator:
            _unverified()
        result[key] = val
    return result


def verify_systemd(props: dict[str, str], manager_env: str, home: Path, *, system: bool) -> None:
    import pwd

    if not all(key in props for key in SYSTEMD_IDENTITY_PROPERTIES):
        _unverified()
    # These can change the execution account, environment, mount namespace, or
    # definitions at start time; show does not expose their resulting identity.
    if props["DynamicUser"] != "no" or any(props[key] for key in (
        "EnvironmentFiles", "PAMName", "RootDirectory", "RootImage", "ExecStartPre", "ExecCondition",
    )):
        _unverified()
    user = props["User"]
    uid = os.getuid()  # windows-footgun: ok — native systemd only
    if user:
        try:
            service_uid = int(user) if user.isdecimal() else pwd.getpwnam(user).pw_uid
        except KeyError:
            _unverified()
    else:
        service_uid = 0 if system else uid
    if service_uid != uid:
        raise ValueError("service_account_mismatch")
    inherited = environment_pairs(manager_env)
    env = inherited if not system else {k: v for k, v in inherited.items() if k in props["PassEnvironment"].split()}
    env.update(environment_pairs(props["Environment"]))
    for item in shlex.split(props["UnsetEnvironment"]):
        key, sep, value = item.partition("=")
        if not sep or env.get(key) == value:
            env.pop(key, None)
    configured = env.get("HERMES_HOME", "").strip()
    if not configured:
        configured = str(_absolute(env.get("HOME") or pwd.getpwuid(uid).pw_dir) / ".hermes")
    verify_home(home, configured)
    match = re.fullmatch(r"\{ path=(.*?) ; argv\[\]=(.*?) ; ignore_errors=(?:yes|no) ;(?:.*?)\}", props["ExecStart"])
    if not match or any(char in match[2] for char in ("$", "\\", "{", "}")):
        _unverified()
    argv = shlex.split(match[2])
    if not argv or match[1] != argv[0]:
        _unverified()
    verify_gateway_argv(argv, home)
