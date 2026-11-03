"""Tabs ``browser_exec`` opened on a shared browser, and closing exactly those when its session ends.

A shared browser (``/browser connect``, ``BROWSER_CDP_URL``, ``browser.cdp_url``, an operator ``BU_CDP_*``
endpoint, the real-profile or a persistent-profile Chromium) outlives every Hermes session, so a tab the
agent leaves open stays open until the user closes it (#21532). Each such call runs with the tab recorder
(``browser_use_tab_recorder``), which reports the exact ``targetId`` of every tab the call created; the
ids are merged here per route key (``browser_use_cli._backend_cache_key``) and closed by the existing
lifecycle: per-turn ``cleanup_browser`` for the task's unnamed routes, the inactivity janitor for named
sessions, ``cleanup_profile_browsers`` before a ``/browser connect`` switch, and ``cleanup_all_browsers``.

Only recorded ids are ever closed, so a tab the user opened, or one the agent merely switched to, stays.
The close runs on its own one-shot browser-level connection (``browser_cdp_tool._cdp_call`` under
``_run_async``), never on a CDP supervisor's loop. ``browser.cdp_close_agent_tabs: false`` turns it off.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from agent.redact import redact_cdp_url
from hermes_constants import get_hermes_home, hermes_home_key
from utils import is_truthy_value

logger = logging.getLogger(__name__)

_CLOSE_TIMEOUT_S = 5.0
# Chrome answers Target.closeTarget before the target is gone: the re-check polls this long for it to go.
_GONE_WITHIN_S = 3.0
# Cleanups that could not reach a browser before its ids are dropped: a browser that stays unreachable
# was closed or restarted (its tabs went with it), and a dead endpoint must not cost every cleanup a dial.
_MAX_UNREACHABLE = 3


def _bs():
    from tools import browser_session
    return browser_session


def close_agent_tabs_enabled() -> bool:
    """``browser.cdp_close_agent_tabs`` (default on)."""
    from tools.browser_session_cloud import browser_cfg
    return bool(browser_cfg("cdp_close_agent_tabs", True, lambda v: is_truthy_value(v, default=True)))


def _endpoint(env: dict) -> str:
    return str(env.get("BU_CDP_WS") or env.get("BU_CDP_URL") or "")


def tab_report_path(env: dict, private_browser: bool) -> Optional[str]:
    """A fresh 0600 report file for one call on a shared CDP browser, or None (private browser, no
    endpoint, or the opt-out is set): the recorder is only installed when this returns a path."""
    if private_browser or not _endpoint(env) or not close_agent_tabs_enabled():
        return None
    try:
        directory = Path(get_hermes_home()) / "cache" / "browser-use" / "tabs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="tabs-", suffix=".json", dir=directory)
    except OSError as exc:
        logger.warning("browser_exec: cannot record the tabs this call opens: %s", exc)
        return None
    os.close(fd)
    return path


def _read_report(path: str) -> tuple[set[str], set[str]]:
    """``(created, closed)`` ids from a report; the file (and a half-written temp) is removed."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError) as exc:
        logger.debug("browser_exec: unreadable tab report %s: %s", path, exc)
        state = {}
    finally:
        for leftover in (path, path + ".tmp"):
            try:
                os.unlink(leftover)
            except OSError:
                pass

    def ids(field: str) -> set[str]:
        value = state.get(field) if isinstance(state, dict) else None
        return {t for t in value if isinstance(t, str) and t} if isinstance(value, list) else set()
    return ids("created"), ids("closed")


def record_tab_report(route_key: str, env: dict, path: Optional[str]) -> None:
    """Merge one call's report into ``route_key``'s owned tabs and touch the route's activity, so the
    inactivity janitor closes them once the route goes idle."""
    if not path:
        return
    created, closed = _read_report(path)
    bs = _bs()
    endpoint = _endpoint(env)
    with bs._cleanup_lock:
        entry = bs._owned_cdp_targets.get(route_key)
        if entry is None and created:
            entry = bs._owned_cdp_targets[route_key] = {"home_key": hermes_home_key(), "targets": {}}
        if entry is not None:
            targets = entry["targets"]
            owned = targets.setdefault(endpoint, set()) if isinstance(targets, dict) else set()
            owned.update(created)
            owned.difference_update(closed)
            _prune(route_key, entry)
        tracked = route_key in bs._owned_cdp_targets
    if tracked:
        from tools.browser_session_lifecycle import start_cleanup_thread, update_session_activity
        start_cleanup_thread()
        update_session_activity(route_key)


def _prune(route_key: str, entry: dict) -> None:
    """Drop empty endpoint sets, and the route once it owns nothing (caller holds the lock)."""
    for endpoint in [ep for ep, ids in entry["targets"].items() if not ids]:
        del entry["targets"][endpoint]
    if not entry["targets"]:
        _bs()._owned_cdp_targets.pop(route_key, None)


def owned_route_keys(match: Callable[[str, dict], bool] = lambda key, entry: True) -> list[str]:
    """Route keys holding owned tabs, filtered by ``match(key, entry)``."""
    bs = _bs()
    with bs._cleanup_lock:
        return [key for key, entry in bs._owned_cdp_targets.items() if match(key, entry)]


def _still_open(owned: set[str], open_ids: Callable[[], set[str]]) -> set[str]:
    deadline = time.monotonic() + _GONE_WITHIN_S
    left = owned & open_ids()
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = owned & open_ids()
    return left


def _close_on(endpoint: str, owned: set[str]) -> Optional[set[str]]:
    """Close ``owned`` tabs that are still open on ``endpoint``; returns the ids still open afterwards,
    or None when the browser could not be asked."""
    from tools.browser_cdp_tool import _WS_AVAILABLE, WebSocketException, _cdp_call, _run_async
    from tools.browser_session_cdp import resolve_cdp_override
    if not _WS_AVAILABLE:
        return None
    ws_url = resolve_cdp_override(endpoint)

    def open_ids() -> set[str]:
        reply = _run_async(_cdp_call(ws_url, "Target.getTargets", {}, None, _CLOSE_TIMEOUT_S))
        return {str(t.get("targetId")) for t in reply.get("targetInfos", []) if isinstance(t, dict)}

    try:
        for target in sorted(owned & open_ids()):
            try:
                _run_async(_cdp_call(ws_url, "Target.closeTarget", {"targetId": target}, None, _CLOSE_TIMEOUT_S))
            except RuntimeError as exc:  # a CDP error reply: the re-check below keeps the id
                logger.debug("Target.closeTarget %s refused: %s", target, exc)
        left = _still_open(owned, open_ids)
    except (OSError, RuntimeError, TimeoutError, ValueError, WebSocketException) as exc:
        logger.warning("Could not close %d browser tab(s) Hermes opened on %s: %s",
                       len(owned), redact_cdp_url(endpoint), redact_cdp_url(str(exc)))
        return None
    if left:
        logger.warning("%d browser tab(s) Hermes opened on %s are still open; retrying at the next cleanup",
                       len(left), redact_cdp_url(endpoint))
    return left


def close_owned_cdp_targets(route_key: str) -> None:
    """Close the tabs ``route_key``'s calls opened and are still open; keep any that would not close."""
    bs = _bs()
    with bs._cleanup_lock:
        entry = bs._owned_cdp_targets.get(route_key)
        snapshot = {ep: set(ids) for ep, ids in (entry or {}).get("targets", {}).items()}
    for endpoint, owned in snapshot.items():
        left = _close_on(endpoint, owned)
        with bs._cleanup_lock:
            current = bs._owned_cdp_targets.get(route_key)
            if current is None:
                continue
            misses = current.setdefault("unreachable", {})
            if left is None:
                misses[endpoint] = misses.get(endpoint, 0) + 1
                if misses[endpoint] < _MAX_UNREACHABLE:
                    continue
                left = set()  # given up on: see _MAX_UNREACHABLE
            misses.pop(endpoint, None)
            current["targets"].get(endpoint, set()).difference_update(owned - left)
            _prune(route_key, current)
