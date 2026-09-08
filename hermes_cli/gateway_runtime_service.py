"""Non-installing service-manager operations for an explicit canonical profile.

The legacy start helpers refresh units, regenerate plists, or fall back to
unmanaged processes. Client startup must not call those helpers.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from gateway.runtime_contract import RuntimeState
from hermes_cli._subprocess_compat import windows_hide_flags


class RuntimeStartError(RuntimeError):
    def __init__(self, reason: str, state: RuntimeState = "inaccessible"):
        super().__init__(reason)
        self.reason, self.state = reason, state


def remaining(deadline: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError
    return budget


def service_suffix(home: Path) -> str:
    from hermes_constants import get_default_hermes_root
    from hermes_cli.gateway import _profile_name_from_home
    default = get_default_hermes_root().resolve()
    home = home.resolve()
    if home == default:
        return ""
    return _profile_name_from_home(home, default) or hashlib.sha256(str(home).encode()).hexdigest()[:8]


@dataclass(frozen=True)
class ExistingService:
    backend: str
    start_argv: tuple[str, ...]
    running: bool = False


def _run(argv: list[str] | tuple[str, ...], deadline: float):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=windows_hide_flags(), timeout=remaining(deadline))


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _systemd(home: Path, deadline: float) -> ExistingService | None:
    from hermes_cli import gateway as gw
    from hermes_cli.service_manager import _s6_running
    from hermes_cli.gateway_runtime_service_identity import SYSTEMD_IDENTITY_PROPERTIES, verify_systemd
    if _s6_running():
        raise RuntimeStartError("external_supervisor")
    suffix = service_suffix(home)
    unit = f"hermes-gateway{'-' + suffix if suffix else ''}.service"
    paths = (Path.home() / ".config/systemd/user" / unit, Path("/etc/systemd/system") / unit)
    # On non-systemd hosts there is no manager to own transient units. Installed
    # definitions still count: a broken manager is not permission to bypass one.
    manager_paths = (Path("/run/systemd/system"),
                     Path(f"/run/user/{os.getuid()}/systemd/private"))  # windows-footgun: ok — native systemd only
    if not any(_exists(p) for p in (*paths, *manager_paths)):
        return None
    found = []
    for system in (False, True):
        command = gw._systemctl_cmd(system)
        result = _run([*command, "show", unit, "--no-pager",
                       "--property=LoadState,ActiveState,SubState,UnitFileState," + ",".join(SYSTEMD_IDENTITY_PROPERTIES)], deadline)
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found" and not _exists(paths[int(system)]):
            continue
        if result.returncode or props.get("LoadState") != "loaded":
            raise RuntimeStartError("service_manager_unavailable")
        if props.get("UnitFileState") in {"masked", "masked-runtime"}:
            raise RuntimeStartError("service_masked")
        state = props.get("ActiveState")
        if state == "deactivating":
            raise RuntimeStartError("service_draining", "draining")
        if state not in {"inactive", "active", "activating", "reloading", "failed"}:
            raise RuntimeStartError("service_state_unknown")
        if state == "failed":
            raise RuntimeStartError("service_failed")
        environment = _run([*command, "show-environment"], deadline)
        if environment.returncode:
            raise RuntimeStartError("service_identity_unverified")
        try:
            verify_systemd(props, environment.stdout, home, system=system)
        except ValueError as exc:
            reason = str(exc)
            raise RuntimeStartError(reason if reason in {
                "profile_mismatch", "service_account_mismatch"
            } else "service_identity_unverified") from None
        found.append(ExistingService("systemd", tuple([*command, "--no-ask-password", "start", unit]),
                                     state != "inactive"))
    if len(found) > 1:
        raise RuntimeStartError("service_scope_conflict", "conflict")
    return found[0] if found else None


def _launchd(home: Path, deadline: float) -> ExistingService | None:
    import pwd
    suffix = service_suffix(home)
    label = f"ai.hermes.gateway{'-' + suffix if suffix else ''}"
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # windows-footgun: ok — native launchd only
    plist = account_home / "Library/LaunchAgents" / f"{label}.plist"
    installed = _exists(plist)
    if installed:
        with plist.open("rb") as stream:
            definition = plistlib.load(stream)
        configured = definition.get("EnvironmentVariables", {}).get("HERMES_HOME")
        if not configured or Path(configured).resolve() != home or definition.get("Label") != label:
            raise RuntimeStartError("profile_mismatch")
        if definition.get("Disabled"):
            raise RuntimeStartError("service_disabled")
    domains = [f"gui/{os.getuid()}", f"user/{os.getuid()}"]  # windows-footgun: ok — native launchd only
    found = []
    for domain in domains:
        result = _run(["launchctl", "print", f"{domain}/{label}"], deadline)
        if result.returncode == 0:
            found.append(ExistingService("launchd", ("launchctl", "kickstart", f"{domain}/{label}"),
                                         "state = running" in result.stdout))
            continue
        # launchctl's native absent-service code; other errors retain uncertainty.
        if result.returncode != 113:
            raise RuntimeStartError("service_manager_unavailable")
    if len(found) > 1:
        raise RuntimeStartError("service_scope_conflict", "conflict")
    if found:
        return found[0]
    if installed:
        # Load ONLY the existing file, never bootout/rewrite or kickstart -k.
        return ExistingService("launchd", ("launchctl", "bootstrap", domains[0], str(plist)))
    return None


def _windows(home: Path, deadline: float) -> ExistingService | None:
    from hermes_cli.gateway_windows import _startup_dir
    suffix = service_suffix(home)
    name = f"Hermes_Gateway{'_' + suffix if suffix else ''}"
    result = _run(["schtasks.exe", "/Query", "/FO", "CSV", "/NH"], deadline)
    if result.returncode:
        raise RuntimeStartError("service_manager_unavailable")
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if any(len(row) != 3 for row in rows if row):
        raise RuntimeStartError("service_state_unknown")
    if any(row and row[0].lstrip("\\") == name for row in rows):
        return ExistingService("windows", ("schtasks.exe", "/Run", "/TN", name))
    # Startup-folder entries are installed persistence too, but have no independent
    # start supervisor. Do not bypass them with a job-bound unmanaged child.
    if any(_exists(_startup_dir() / f"{name}.{ext}") for ext in ("cmd", "vbs")):
        raise RuntimeStartError("startup_service_requires_login")
    return None


def discover_existing_gateway_service(home: Path, *, deadline: float) -> ExistingService | None:
    backend = {"linux": _systemd, "darwin": _launchd, "win32": _windows}.get(sys.platform)
    if backend is None:
        raise RuntimeStartError("unsupported_supervisor_platform")
    try:
        return backend(home, deadline)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except (OSError, ValueError) as exc:
        raise RuntimeStartError("service_manager_unavailable") from exc


def start_existing_gateway_service(service: ExistingService, *, deadline: float) -> None:
    """Request start once; only live control readiness can establish success."""
    if service.running:
        return
    try:
        result = _run(service.start_argv, deadline)
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    except OSError as exc:
        raise RuntimeStartError("service_start_failed") from exc
    if result.returncode:
        raise RuntimeStartError("service_start_failed")
