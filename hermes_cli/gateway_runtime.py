"""Validated, credential-free gateway endpoint discovery for local clients."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from gateway.runtime_contract import RuntimeState


@dataclass(frozen=True)
class GatewayEndpoint:
    profile_id: str
    instance_id: str
    authority_epoch: int
    runtime_protocol: int
    api_origin: str
    supervisor: Literal["none", "systemd", "launchd", "windows", "external"]
    capabilities: frozenset[str]


@dataclass(frozen=True)
class GatewayDiscovery:
    state: RuntimeState
    endpoint: GatewayEndpoint | None = None
    reason_code: str | None = None


def _canonical_home(home: str | Path) -> str:
    return os.path.normcase(str(Path(home).expanduser().resolve()))


def _endpoint(payload: dict, home: Path) -> GatewayDiscovery:
    if type(payload.get("runtime_protocol")) is not int or payload["runtime_protocol"] != 1:
        return GatewayDiscovery("incompatible", reason_code="runtime_protocol")
    profiles = payload.get("served_profiles")
    if not isinstance(profiles, list):
        return GatewayDiscovery("inaccessible", reason_code="profile_mismatch")
    matches = [p for p in profiles if isinstance(p, dict)
               and isinstance(p.get("home"), str) and _canonical_home(p["home"]) == str(home)]
    if len(matches) != 1 or not isinstance(matches[0].get("profile_id"), str) or not matches[0]["profile_id"]:
        return GatewayDiscovery("inaccessible", reason_code="profile_mismatch")
    state = payload.get("state")
    if state in {"starting", "draining", "conflict"}:
        return GatewayDiscovery(state)
    if state != "ready":
        return GatewayDiscovery("inaccessible", reason_code="invalid_runtime_state")
    capabilities = payload.get("capabilities")
    if (not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities)
            or "session-authority-v1" not in capabilities):
        return GatewayDiscovery("incompatible", reason_code="session_authority_unavailable")
    epoch, instance = payload.get("authority_epoch"), payload.get("instance_id")
    if type(epoch) is not int or epoch <= 0 or not isinstance(instance, str) or not instance:
        return GatewayDiscovery("inaccessible", reason_code="invalid_runtime_identity")
    origin = payload.get("api_origin")
    if not isinstance(origin, str):
        return GatewayDiscovery("inaccessible", reason_code="invalid_api_origin")
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or not parsed.port
                or not ipaddress.ip_address(parsed.hostname).is_loopback):
            return GatewayDiscovery("inaccessible", reason_code="invalid_api_origin")
    except ValueError:
        return GatewayDiscovery("inaccessible", reason_code="invalid_api_origin")
    supervisor = payload.get("supervisor")
    if supervisor not in {"none", "systemd", "launchd", "windows", "external"}:
        return GatewayDiscovery("inaccessible", reason_code="invalid_supervisor")
    return GatewayDiscovery("ready", GatewayEndpoint(
        profile_id=matches[0]["profile_id"], instance_id=instance,
        authority_epoch=epoch, runtime_protocol=1, api_origin=origin,
        supervisor=supervisor, capabilities=frozenset(capabilities),
    ))


def discover_gateway_endpoint(profile_home: str | Path, *, timeout: float = 2.0) -> GatewayDiscovery:
    """Read live control identity, preserving uncertainty instead of spawning."""
    from hermes_cli.gateway_runtime_discovery import DiscoveryError, missing_owner_state, query_identify

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    home = Path(_canonical_home(profile_home))
    try:
        return _endpoint(query_identify(home, timeout=timeout), home)
    except (FileNotFoundError, ConnectionRefusedError):
        return GatewayDiscovery(missing_owner_state(home))
    except TimeoutError:
        return GatewayDiscovery("inaccessible", reason_code="control_timeout")
    except DiscoveryError as exc:
        return GatewayDiscovery("inaccessible", reason_code=exc.reason)
    except PermissionError:
        return GatewayDiscovery("inaccessible", reason_code="authorization")
    except (OSError, ValueError, TypeError):
        # Raw peer data, paths and URLs may contain secrets; report bounded codes.
        return GatewayDiscovery("inaccessible", reason_code="invalid_control_peer")


def ensure_gateway_runtime(profile_home: str | Path, *, timeout: float = 30.0) -> GatewayDiscovery:
    """Ensure once, never install/replace; pending remains pending at deadline.

    A successful service command or Popen is not session readiness. After an
    owner/start request is observed this invocation never launches another.
    """
    import time
    from hermes_cli.gateway_runtime_service import (
        RuntimeStartError, discover_existing_gateway_service, remaining,
        start_existing_gateway_service,
    )
    from hermes_cli.update_lock import MARKER_NAME
    from hermes_constants import get_default_hermes_root, get_process_hermes_home

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    home = Path(_canonical_home(profile_home))
    requested = False
    delay = 0.025
    try:
        while True:
            # Never clear the updater's fence, even if malformed. Repair is not
            # a client operation. The install-root marker also covers profiles.
            for fence_home in {home, get_process_hermes_home(), get_default_hermes_root()}:
                try:
                    (fence_home / MARKER_NAME).lstat()
                except FileNotFoundError:
                    continue
                return GatewayDiscovery("draining", reason_code="update_paused")
            observed = discover_gateway_endpoint(home, timeout=remaining(deadline))
            if observed.reason_code == "control_timeout":
                return GatewayDiscovery("starting", reason_code="deadline")
            if observed.state not in {"absent", "starting"}:
                return observed
            if observed.state == "starting":
                requested = True
            if observed.state == "absent" and not requested:
                service = discover_existing_gateway_service(home, deadline=deadline)
                # Runtime locks settle races remaining after this second probe.
                observed = discover_gateway_endpoint(home, timeout=remaining(deadline))
                if observed.state != "absent":
                    continue
                if service is not None:
                    start_existing_gateway_service(service, deadline=deadline)
                else:
                    from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
                    spawn_unmanaged_gateway(home, deadline=deadline)
                requested = True
            time.sleep(min(delay, remaining(deadline)))
            delay = min(delay * 1.5, 0.25)
    except TimeoutError:
        return GatewayDiscovery("starting", reason_code="deadline")
    except RuntimeStartError as exc:
        return GatewayDiscovery(exc.state, reason_code=exc.reason)
    except PermissionError:
        return GatewayDiscovery("inaccessible", reason_code="authorization")
    except OSError:
        return GatewayDiscovery("inaccessible", reason_code="runtime_start_failed")
