"""Tabs browser_exec opens on a shared browser close when its session is cleaned up, and no others do.

Regression for #21532: on a ``/browser connect`` / ``browser.cdp_url`` browser every ``new_tab()`` stayed
open forever, because cleanup only ever released browsers Hermes launched. The recorder runs inside the
real browser-harness CLI (only its daemon transport is faked); the close runs against an in-process CDP
WebSocket server, so the real ``Target.getTargets`` / ``Target.closeTarget`` traffic is what is asserted.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

import tools.browser_use_cli as bu_cli
from tests.tools.test_browser_cdp_tool import _CDPServer
from tools import browser_session as bs
from tools import browser_session_cloud as bs_cloud
from tools import browser_session_lifecycle as lifecycle
from tools import browser_session_owned_tabs as owned_tabs
from tools import browser_use_tab_recorder as recorder
from tests.waiting import wait_until

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
        if method == "Page.captureScreenshot":
            return {"data": "aGVybWVz"}
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
                 "_owned_cdp_targets", "_routes_in_flight"):
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


def test_an_unwritable_report_never_fails_the_models_code(run_exec, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(owned_tabs, "tab_report_path", lambda endpoint, private: str(blocker / "tabs.json"))
    shot = tmp_path / "shot.png"

    result = run_exec("ws://127.0.0.1:1/devtools/browser/x",
                      f"new_tab(); new_tab(); print('SHOT', capture_screenshot({str(shot)!r}))")

    assert result["success"] is True, result
    assert f"SHOT {shot}" in result["output"] and shot.read_bytes() == b"hermes"
    assert result["stderr"].count("cannot record the tabs") == 1


def test_the_report_variable_reaches_neither_the_models_code_nor_its_subprocesses(run_exec):
    code = ("import os, subprocess, sys\n"
            "print('SELF', os.environ.get('BH_HERMES_TAB_REPORT'))\n"
            "print(subprocess.run([sys.executable, '-c', \"import os; print('CHILD', os.environ.get('BH_HERMES_TAB_REPORT'))\"],"
            " capture_output=True, text=True).stdout.strip())\n"
            "new_tab()")
    result = run_exec("ws://127.0.0.1:1/devtools/browser/x", code)

    assert result["success"] is True, result
    assert "SELF None" in result["output"] and "CHILD None" in result["output"]
    assert _owned("turn-1") == {"NEW-1"}  # the recorder still holds its path


def test_a_report_variable_in_hermess_own_environment_is_never_passed_on(run_exec, monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text("browser:\n  cdp_close_agent_tabs: false\n", encoding="utf-8")
    stale = tmp_path / "stale.json"
    monkeypatch.setenv(recorder.REPORT_ENV, str(stale))

    result = run_exec("ws://127.0.0.1:1/devtools/browser/x",
                      "import os; new_tab(); print('SEEN', os.environ.get('BH_HERMES_TAB_REPORT'))")

    assert result["success"] is True, result
    assert "SEEN None" in result["output"]
    assert not stale.exists()


def test_concurrent_cdp_calls_record_every_tab(tmp_path, monkeypatch):
    """Two threads in one call: each event's rewrite is serialised, so no write loses another's tab."""
    report = tmp_path / "tabs.json"
    monkeypatch.setenv(recorder.REPORT_ENV, str(report))
    ids = iter(["T-1", "T-2"])
    helpers = types.SimpleNamespace(cdp=lambda method, **params: {"targetId": next(ids)})
    recorder.install(helpers)
    persist, inside, entries, overlapped = recorder._persist, [], [], threading.Event()

    def slow_persist(path, state):
        inside.append(1)
        entries.append(1)
        if len(inside) > 1:  # another writer is in here too
            overlapped.set()
        elif len(entries) == 1:
            overlapped.wait(2.5)  # the first writer holds on, giving the other every chance to enter
        try:
            persist(path, state)
        finally:
            inside.pop()
    monkeypatch.setattr(recorder, "_persist", slow_persist)
    results, errors = [], []

    def create():
        try:
            results.append(helpers.cdp("Target.createTarget", url="about:blank"))
        except Exception as exc:  # health: allow BLE001 -- surfaced by the assertion below
            errors.append(exc)
    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == [] and sorted(r["targetId"] for r in results) == ["T-1", "T-2"]
    assert not overlapped.is_set()
    assert sorted(json.loads(report.read_text(encoding="utf-8"))["created"]) == ["T-1", "T-2"]


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


def _age(route_key: str) -> None:
    bs._session_last_activity[route_key] = time.time() - bs.BROWSER_SESSION_INACTIVITY_TIMEOUT - 5


def test_a_tab_the_browser_refuses_to_close_is_retried_by_the_janitor(browser):
    b = browser(USER_TAB, "OWNED-A", "OWNED-C")
    b.refuse.add("OWNED-C")
    _own("bu-named-r7k2", b.url, "OWNED-A", "OWNED-C")
    _age("bu-named-r7k2")

    lifecycle.cleanup_inactive_sessions()
    assert b.open == {USER_TAB, "OWNED-C"}
    assert _owned("bu-named-r7k2") == {"OWNED-C"}
    assert "bu-named-r7k2" in bs._session_last_activity  # still scheduled

    b.refuse.clear()
    _age("bu-named-r7k2")
    lifecycle.cleanup_inactive_sessions()

    assert b.closed() == ["OWNED-A", "OWNED-C", "OWNED-C"]
    assert b.open == {USER_TAB}
    assert bs._owned_cdp_targets == {} and bs._session_last_activity == {}


def _dead_endpoint() -> str:
    with socket.socket() as probe:  # a port nothing listens on any more
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"ws://127.0.0.1:{port}/devtools/browser/gone"


def test_the_janitor_drops_an_unreachable_browsers_ids_after_three_passes():
    _own("bu-named-r7k2", _dead_endpoint(), "OWNED-A")
    lifecycle.update_session_activity("bu-named-r7k2")

    for _ in range(owned_tabs._MAX_UNREACHABLE - 1):
        _age("bu-named-r7k2")
        lifecycle.cleanup_inactive_sessions()
        assert _owned("bu-named-r7k2") == {"OWNED-A"}
        assert "bu-named-r7k2" in bs._session_last_activity
    _age("bu-named-r7k2")
    lifecycle.cleanup_inactive_sessions()

    assert bs._owned_cdp_targets == {}
    assert bs._session_last_activity == {} and bs._session_owner_homes == {}


def test_a_close_that_stalls_does_not_stop_the_rest_of_the_pass(browser, monkeypatch):
    from tools import browser_cdp_tool
    monkeypatch.setattr(owned_tabs, "_GONE_WITHIN_S", 0.5)
    b = browser(USER_TAB, "A-STALLS", "B-CLOSES")
    real_call = browser_cdp_tool._cdp_call

    async def cdp_call(ws_url, method, params, target_id, timeout):
        if method == "Target.closeTarget" and params["targetId"] == "A-STALLS":
            raise TimeoutError("Timed out waiting for Target.closeTarget")  # what _cdp_call raises on a stall
        return await real_call(ws_url, method, params, target_id, timeout)
    monkeypatch.setattr(browser_cdp_tool, "_cdp_call", cdp_call)
    _own("turn-1", b.url, "A-STALLS", "B-CLOSES")

    owned_tabs.close_owned_cdp_targets("turn-1")

    assert b.open == {USER_TAB, "A-STALLS"}
    assert _owned("turn-1") == {"A-STALLS"}
    assert "unreachable" not in bs._owned_cdp_targets["turn-1"]  # the browser answered: not a miss


class _Blackhole:
    """A port that accepts connections and never answers (a hung or firewalled CDP endpoint)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.accepted: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.sock.getsockname()[1]}/devtools/browser/hung"

    def _accept(self):
        while True:
            try:
                self.accepted.append(self.sock.accept()[0])
            except OSError:
                return

    def close(self):
        self.sock.close()
        for conn in self.accepted:
            conn.close()


@pytest.mark.parametrize("sweep", ["exit", "turn"])
def test_a_sweep_dials_a_hung_browser_once_for_all_its_routes(monkeypatch, sweep):
    monkeypatch.setattr(owned_tabs, "_CLOSE_TIMEOUT_S", 2.0)
    monkeypatch.setattr("tools.browser_launcher.stop_all_browsers", lambda: None)
    hung = _Blackhole()
    try:
        for n, key in enumerate(("turn-1", "turn-1@home-a", "turn-1@home-b")):
            _own(key, hung.url, f"TAB-{n}")

        lifecycle.cleanup_all_browsers() if sweep == "exit" else lifecycle.cleanup_browser("turn-1")

        assert len(hung.accepted) == 1
        assert {key: entry["unreachable"] for key, entry in bs._owned_cdp_targets.items()} == {
            key: {hung.url: 1} for key in ("turn-1", "turn-1@home-a", "turn-1@home-b")}
    finally:
        hung.close()


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


def test_the_janitor_never_closes_a_route_while_a_call_drives_it(run_exec, browser, monkeypatch):
    """A browser_exec call may run past the inactivity timeout: its route is in flight, not idle."""
    b = browser(USER_TAB)
    assert run_exec(b.url, "new_tab()", session="r7k2")["success"] is True
    b.open.add("NEW-1")
    real_run, seen = bu_cli._run_cli_killing_process_group, []

    def long_call(cmd, code, env, timeout):
        _age("bu-named-r7k2")  # the call has outlasted the inactivity timeout
        lifecycle.cleanup_inactive_sessions()
        seen.append(set(b.open))
        return real_run(cmd, code, env, timeout)
    monkeypatch.setattr(bu_cli, "_run_cli_killing_process_group", long_call)
    assert run_exec(b.url, "print('still working')", session="r7k2")["success"] is True

    assert seen == [{USER_TAB, "NEW-1"}] and b.closed() == []
    assert bs._routes_in_flight == {}
    _age("bu-named-r7k2")
    lifecycle.cleanup_inactive_sessions()
    assert b.open == {USER_TAB}


def test_a_route_released_from_flight_is_closed_again(browser, monkeypatch):
    stopped = []
    monkeypatch.setattr("tools.browser_session_cdp.stop_cdp_supervisor", stopped.append)
    b = browser(USER_TAB, "A")
    _own("bu-named-r7k2", b.url, "A")
    _age("bu-named-r7k2")
    with owned_tabs.route_call("bu-named-r7k2"):
        assert time.time() - bs._session_last_activity["bu-named-r7k2"] < 60  # touched as the call starts
        _age("bu-named-r7k2")
        lifecycle.cleanup_inactive_sessions()
        assert stopped == []  # the janitor skips the in-flight route entirely, not just its tabs
        lifecycle.cleanup_single_session("bu-named-r7k2")
        assert b.closed() == []
    _age("bu-named-r7k2")
    lifecycle.cleanup_inactive_sessions()

    assert b.open == {USER_TAB}


def test_exit_cleanup_closes_a_route_even_with_a_call_in_flight(browser, monkeypatch):
    monkeypatch.setattr("tools.browser_launcher.stop_all_browsers", lambda: None)
    b = browser(USER_TAB, "A")
    _own("bu-named-r7k2", b.url, "A")
    with owned_tabs.route_call("bu-named-r7k2"):
        lifecycle.cleanup_all_browsers()

    assert b.open == {USER_TAB}


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


# ── 5. Session names ────────────────────────────────────────────────────────────────────────────────

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
