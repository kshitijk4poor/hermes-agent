"""Tabs browser_exec opens on a shared browser close when its session is cleaned up, and no others do.

Regression for #21532: on a ``/browser connect`` / ``browser.cdp_url`` browser every ``new_tab()`` stayed
open forever, because cleanup only ever released browsers Hermes launched. The recorder runs inside the
real browser-harness CLI (only its daemon transport is faked); the close runs against an in-process CDP
WebSocket server, so the real ``Target.getTargets`` / ``Target.closeTarget`` traffic is what is asserted.
"""
import asyncio
import json
import os
import sys
import textwrap
import time

import pytest

import tools.browser_use_cli as bu_cli
from tests.tools.test_browser_cdp_tool import _CDPServer
from tools import browser_session as bs
from tools import browser_session_cloud as bs_cloud
from tools import browser_session_lifecycle as lifecycle
from tools import browser_session_owned_tabs as owned_tabs

USER_TAB = "USER-TAB"

# sitecustomize on the harness's PYTHONPATH: the real browser_harness CLI runs (helpers, new_tab, close_tab,
# switch_tab), only its daemon transport is faked. Created targets are numbered NEW-1, NEW-2, ...
_FAKE_TRANSPORT = textwrap.dedent("""\
    import itertools, json, os
    import browser_harness.helpers as h

    LOG = os.environ["FAKE_CDP_LOG"]
    ids = itertools.count(int(os.environ.get("FAKE_FIRST_ID", "1")))

    def cdp(method, session_id=None, _response_timeout=5.0, **params):
        with open(LOG, "a") as f:
            f.write(json.dumps({"method": method, **params}) + "\\n")
        if method == "Target.createTarget":
            return {"targetId": "NEW-%d" % next(ids)}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-" + params["targetId"]}
        if method == "Target.closeTarget":
            return {"success": True}
        return {}

    h.cdp = cdp
    h._send = lambda req, **kw: {}
    h.current_tab = lambda: {"targetId": "USER-TAB", "target_id": "USER-TAB",
                             "url": "https://user.example/", "title": "user"}
    import browser_harness.admin as admin, browser_harness.telemetry as telemetry
    admin.ensure_daemon = admin.print_update_banner = lambda *a, **k: None
    admin.daemon_alive = lambda *a, **k: True
    telemetry.is_enabled = lambda: False
    telemetry.capture_cli_event = lambda *a, **k: None
""")


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    for name in ("_active_sessions", "_session_last_activity", "_session_owner_homes", "_cleanup_failures",
                 "_owned_cdp_targets"):
        monkeypatch.setattr(bs, name, {})
    monkeypatch.setattr(lifecycle, "start_cleanup_thread", lambda: None)  # the janitor is driven by hand
    monkeypatch.setattr(bu_cli, "_driven_daemons", {})
    monkeypatch.delenv("BU_NAME", raising=False)
    monkeypatch.delenv("BU_AUTOSPAWN", raising=False)
    bs_cloud.reset_caches()
    yield
    bs_cloud.reset_caches()


class _Browser:
    """An in-process CDP endpoint whose open page targets are ``self.open``."""

    def __init__(self, server: _CDPServer, url: str, *targets: str):
        self.server, self.url, self.open = server, url, set(targets)
        self.refuse: set[str] = set()
        server.on("Target.getTargets", lambda p, s: {"targetInfos": [
            {"targetId": t, "type": "page", "url": "about:blank"} for t in sorted(self.open)]})
        server.on("Target.closeTarget", self._close)

    def _close(self, params, _session):
        target = params["targetId"]
        if target in self.refuse:
            return RuntimeError(f"cannot close {target}")
        self.open.discard(target)
        return {"success": True}

    def closed(self) -> list[str]:
        return [m["params"]["targetId"] for m in self.server.received() if m["method"] == "Target.closeTarget"]


@pytest.fixture
def browser():
    servers = []

    def start(*targets: str) -> _Browser:
        server = _CDPServer()
        url = server.start()
        servers.append(server)
        return _Browser(server, url, *targets)

    yield start
    for server in servers:
        server.stop()


@pytest.fixture
def run_exec(monkeypatch, tmp_path):
    """browser_exec on the CDP-override route, through the real harness CLI with the fake transport."""
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    monkeypatch.setattr(bu_cli, "_exec_route_is_sidecar", {})
    monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: None)
    fake_dir = tmp_path / "fake_transport"
    fake_dir.mkdir()
    (fake_dir / "sitecustomize.py").write_text(_FAKE_TRANSPORT, encoding="utf-8")
    site_dir = bu_cli._harness_site_dir()
    assert site_dir, "browser-harness is a core dependency of the test environment"
    monkeypatch.setattr(bu_cli, "_harness_site_dir", lambda: os.pathsep.join([str(fake_dir), site_dir]))
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [sys.executable, "-m", "browser_harness.run"])
    log = tmp_path / "cdp.jsonl"
    monkeypatch.setenv("FAKE_CDP_LOG", str(log))

    def run(endpoint: str, code: str, *, task_id: str = "turn-1", session: str = "", first_id: int = 1):
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: endpoint)
        monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override_raw", lambda: endpoint)
        monkeypatch.setenv("FAKE_FIRST_ID", str(first_id))
        return json.loads(bu_cli.browser_exec(code, session=session, task_id=task_id, timeout_s=60))

    return run


def _owned(route_key: str) -> set[str]:
    entry = bs._owned_cdp_targets.get(route_key) or {"targets": {}}
    return set().union(*entry["targets"].values()) if entry["targets"] else set()


# ── 1. Recorder: exact created ids, from inside the real harness ────────────────────────────────────

def test_new_tab_records_exactly_the_created_tab_and_close_tab_removes_it(run_exec):
    endpoint = "ws://127.0.0.1:1/devtools/browser/x"
    result = run_exec(endpoint, "a = new_tab(); b = new_tab(); close_tab(a); print('TABS', a, b)")

    assert result["success"] is True, result
    assert "TABS NEW-1 NEW-2" in result["output"]
    assert bs._owned_cdp_targets["turn-1"]["targets"] == {endpoint: {"NEW-2"}}


def test_switching_to_the_users_tab_records_nothing(run_exec):
    result = run_exec("ws://127.0.0.1:1/devtools/browser/x", f"switch_tab({USER_TAB!r}); print('ok')")

    assert result["success"] is True, result
    assert "turn-1" not in bs._owned_cdp_targets


def test_a_call_that_raises_or_dies_still_reports_its_tabs(run_exec):
    raised = run_exec("ws://127.0.0.1:1/devtools/browser/x", "new_tab()\nraise RuntimeError('boom')")
    died = run_exec("ws://127.0.0.1:1/devtools/browser/x", "new_tab()\nimport os; os._exit(3)", first_id=7)

    assert raised["success"] is False and died["exit_code"] == 3
    assert _owned("turn-1") == {"NEW-1", "NEW-7"}


def test_a_later_call_closing_an_earlier_calls_tab_releases_it(run_exec):
    run_exec("ws://127.0.0.1:1/devtools/browser/x", "new_tab()", session="r7k2")
    run_exec("ws://127.0.0.1:1/devtools/browser/x", "close_tab('NEW-1')", session="r7k2")

    assert "bu-named-r7k2" not in bs._owned_cdp_targets


def test_a_private_provider_browser_records_nothing(run_exec, monkeypatch):
    monkeypatch.setattr(bs_cloud, "get_cloud_provider", lambda: object())
    monkeypatch.setattr(bs, "get_session_info", lambda key: {"cdp_url": "ws://127.0.0.1:1/devtools/browser/p"})
    result = run_exec("", "new_tab()")

    assert result["success"] is True, result
    assert bs._owned_cdp_targets == {}


# ── 2. Close selection ──────────────────────────────────────────────────────────────────────────────

def _own(route_key: str, endpoint: str, *targets: str, home_key: str = "home") -> None:
    bs._owned_cdp_targets[route_key] = {"home_key": home_key, "targets": {endpoint: set(targets)}}


def test_close_sends_closetarget_only_for_owned_tabs_still_open(browser):
    b = browser(USER_TAB, "OWNED-A")
    _own("turn-1", b.url, "OWNED-A", "OWNED-B-ALREADY-GONE")

    owned_tabs.close_owned_cdp_targets("turn-1")

    assert b.closed() == ["OWNED-A"]
    assert b.open == {USER_TAB}
    assert "turn-1" not in bs._owned_cdp_targets


def test_a_tab_the_browser_refuses_to_close_stays_registered(browser):
    b = browser(USER_TAB, "OWNED-A", "OWNED-C")
    b.refuse.add("OWNED-C")
    _own("turn-1", b.url, "OWNED-A", "OWNED-C")

    owned_tabs.close_owned_cdp_targets("turn-1")

    assert b.open == {USER_TAB, "OWNED-C"}
    assert _owned("turn-1") == {"OWNED-C"}  # retried at the next cleanup


def test_an_unreachable_browser_keeps_its_ids_for_a_bounded_number_of_cleanups():
    import socket
    with socket.socket() as probe:  # a port nothing listens on any more
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    _own("turn-1", f"ws://127.0.0.1:{port}/devtools/browser/gone", "OWNED-A")

    for _ in range(owned_tabs._MAX_UNREACHABLE - 1):
        owned_tabs.close_owned_cdp_targets("turn-1")
        assert _owned("turn-1") == {"OWNED-A"}
    owned_tabs.close_owned_cdp_targets("turn-1")
    assert "turn-1" not in bs._owned_cdp_targets


# ── 3. Lifecycle wiring ─────────────────────────────────────────────────────────────────────────────

def test_per_turn_cleanup_closes_the_turns_tabs_with_no_session_record(run_exec, browser):
    b = browser(USER_TAB)
    for n in range(1, 6):  # five calls, each opening a tab, as models do
        result = run_exec(b.url, "new_tab()", first_id=n)
        assert result["success"] is True, result
        b.open.add(f"NEW-{n}")  # the fake transport's tab, now open in the browser
    assert bs._active_sessions == {}

    lifecycle.cleanup_browser("turn-1")

    assert b.open == {USER_TAB}
    assert sorted(b.closed()) == [f"NEW-{n}" for n in range(1, 6)]
    assert bs._owned_cdp_targets == {}


def test_named_session_tabs_survive_the_turn_and_close_after_inactivity(browser):
    b = browser(USER_TAB, "NAMED-TAB", "TURN-TAB")
    _own("bu-named-r7k2", b.url, "NAMED-TAB")
    _own("turn-1", b.url, "TURN-TAB")
    lifecycle.update_session_activity("bu-named-r7k2")

    lifecycle.cleanup_browser("turn-1")
    assert b.open == {USER_TAB, "NAMED-TAB"}

    bs._session_last_activity["bu-named-r7k2"] = time.time() - bs.BROWSER_SESSION_INACTIVITY_TIMEOUT - 5
    lifecycle.cleanup_inactive_sessions()
    assert b.open == {USER_TAB}
    assert bs._owned_cdp_targets == {}


def test_a_named_sessions_calls_alone_make_the_janitor_close_its_tabs(run_exec, browser):
    """No session record and no hand-seeded activity: browser_exec's own report is what the janitor sees."""
    b = browser(USER_TAB)
    result = run_exec(b.url, "new_tab()", session="r7k2")
    b.open.add("NEW-1")
    assert result["success"] is True, result

    lifecycle.cleanup_inactive_sessions()  # still fresh: nothing closes
    assert b.open == {USER_TAB, "NEW-1"}
    bs._session_last_activity["bu-named-r7k2"] -= bs.BROWSER_SESSION_INACTIVITY_TIMEOUT + 5
    lifecycle.cleanup_inactive_sessions()

    assert b.open == {USER_TAB}


def test_browser_connect_switch_closes_tabs_on_the_old_endpoint(browser, monkeypatch):
    from hermes_constants import hermes_home_key
    old, new = browser(USER_TAB, "OLD-TAB"), browser("NEW-USER-TAB")
    _own("bu-named-r7k2", old.url, "OLD-TAB", home_key=hermes_home_key())
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: new.url)

    lifecycle.cleanup_profile_browsers(hermes_home_key())

    assert old.open == {USER_TAB}
    assert new.server.received() == []


def test_exit_cleanup_closes_every_routes_tabs(browser, monkeypatch):
    monkeypatch.setattr("tools.browser_launcher.stop_all_browsers", lambda: None)
    b = browser(USER_TAB, "A", "B")
    _own("turn-1", b.url, "A")
    _own("bu-named-r7k2", b.url, "B")

    lifecycle.cleanup_all_browsers()

    assert b.open == {USER_TAB}
    assert bs._owned_cdp_targets == {}


# ── 4. Loop safety ──────────────────────────────────────────────────────────────────────────────────

def test_close_works_from_a_thread_with_a_running_event_loop(browser):
    b = browser(USER_TAB, "A")
    _own("turn-1", b.url, "A")

    async def gateway_turn_end():
        lifecycle.cleanup_browser("turn-1")  # the gateway calls cleanup with its loop running

    asyncio.run(gateway_turn_end())

    assert b.open == {USER_TAB}


def test_close_never_goes_through_a_cdp_supervisor(browser, monkeypatch):
    from tools import browser_supervisor

    class _Registry:
        def stop(self, key):
            stopped.append(key)

        def __getattr__(self, name):
            raise AssertionError(f"supervisor registry used for the close: {name}")

    stopped = []
    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", _Registry())
    b = browser(USER_TAB, "A")
    _own("turn-1", b.url, "A")

    lifecycle.cleanup_browser("turn-1")

    assert b.open == {USER_TAB} and stopped == ["turn-1"]


# ── 5. The own-tab preamble is gone ─────────────────────────────────────────────────────────────────

def test_named_session_on_a_shared_browser_gets_the_models_code_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.browser_session_cdp.get_cdp_override", lambda: "ws://127.0.0.1:1/devtools/browser/x")
    monkeypatch.setattr(bu_cli, "_attach_vault_supervisor", lambda env, task_id: None)
    cli = tmp_path / "browser-use"
    cli.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(cli)])

    result = json.loads(bu_cli.browser_exec("print('payload')", session="r7k2"))

    assert result["output"] == "print('payload')"  # no Target.createTarget prepended


def test_session_named_default_is_rejected(monkeypatch):
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: ["/bin/true"])
    result = json.loads(bu_cli.browser_exec("print(1)", session="default"))

    assert "reserved" in result["error"]


# ── 6. Profiles ─────────────────────────────────────────────────────────────────────────────────────

def test_a_profile_switch_closes_only_that_profiles_tabs(browser):
    b = browser(USER_TAB, "A-TAB", "B-TAB")
    _own("turn-1@home-a", b.url, "A-TAB", home_key="home-a")
    _own("turn-1@home-b", b.url, "B-TAB", home_key="home-b")

    lifecycle.cleanup_profile_browsers("home-a")

    assert b.open == {USER_TAB, "B-TAB"}
    assert set(bs._owned_cdp_targets) == {"turn-1@home-b"}


# ── 7. Opt-out ──────────────────────────────────────────────────────────────────────────────────────

def test_cdp_close_agent_tabs_false_records_and_closes_nothing(run_exec, browser):
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text("browser:\n  cdp_close_agent_tabs: false\n", encoding="utf-8")
    b = browser(USER_TAB)

    result = run_exec(b.url, "new_tab()")
    b.open.add("NEW-1")
    lifecycle.cleanup_browser("turn-1")

    assert result["success"] is True, result
    assert b.open == {USER_TAB, "NEW-1"} and b.server.received() == []
