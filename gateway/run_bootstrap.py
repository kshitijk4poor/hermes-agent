"""Process startup phases; diagnostics remain in the gateway facade."""
from __future__ import annotations

import threading
from typing import Optional

async def _start_gateway_replace_existing_instance(existing_pid: int, replace: bool) -> bool:
    """Handle a live gateway PID under this HERMES_HOME: replace it (``--replace``) or refuse.
    Returns False when startup must abort (refused, permission denied, target still alive)."""
    from gateway.run import (_clear_takeover_marker_quiet, _replace_target_belongs_to_other_profile, _wait_for_pid_exit, get_hermes_home, logger, suppress)
    from gateway.status import get_process_start_time, remove_pid_file, terminate_pid
    if not replace:
        hermes_home = str(get_hermes_home())
        logger.error(
            "Another gateway instance is already running (PID %d, HERMES_HOME=%s). "
            "Use 'hermes gateway restart' to replace it, or 'hermes gateway stop' first.",
            existing_pid, hermes_home)
        print(
            f"\n❌ Gateway already running (PID {existing_pid}).\n"
            f"   Use 'hermes gateway restart' to replace it,\n"
            f"   or 'hermes gateway stop' to kill it first.\n"
            f"   Or use 'hermes gateway run --replace' to auto-replace.\n")
        return False

    # Never signal a process not provably ours (a poisoned PID record → cross-profile restart loop).
    if _replace_target_belongs_to_other_profile(existing_pid):
        from gateway.status import _get_process_hermes_home
        logger.error(
            "Refusing --replace: PID %d cannot be proven to belong "
            "to this profile's gateway (HERMES_HOME %s). Remove the "
            "stale PID record or stop the owning profile explicitly.",
            existing_pid, _get_process_hermes_home())
        return False
    existing_start_time = get_process_start_time(existing_pid)
    logger.info("Replacing existing gateway instance (PID %d) with --replace.", existing_pid)
    # Takeover marker: target exits 0 on our SIGTERM (exit 1 → systemd Restart=on-failure flap loop).
    try:
        from gateway.status import write_takeover_marker
        write_takeover_marker(existing_pid)
    except Exception as e:
        logger.debug("Could not write takeover marker: %s", e)
    # Snapshot children BEFORE signalling: reparented orphans are invisible yet hold scoped token locks.
    try:
        from gateway.status import _snapshot_gateway_children
        _old_gateway_children = _snapshot_gateway_children(existing_pid)
    except Exception:
        _old_gateway_children = []
    try:
        terminate_pid(existing_pid, force=False)
    except ProcessLookupError:
        pass  # Already gone
    except (PermissionError, OSError):
        logger.error("Permission denied killing PID %d. Cannot replace.", existing_pid)
        _clear_takeover_marker_quiet()
        return False
    # Up to 10s for SIGTERM, then SIGKILL.
    if not await _wait_for_pid_exit(existing_pid, 20, 0.5):
        logger.warning("Old gateway (PID %d) did not exit after SIGTERM, sending SIGKILL.", existing_pid)
        old_gateway_exited = False
        try:
            terminate_pid(existing_pid, force=True, expected_start_time=existing_start_time)
        except ProcessLookupError:
            old_gateway_exited = True
        except (PermissionError, OSError):
            pass
        # Confirm SIGKILL took (D-state/zombie) before clearing PID/locks, or two gateways share a token.
        if not old_gateway_exited and not await _wait_for_pid_exit(existing_pid, 20, 0.25):
            logger.error(
                "Old gateway (PID %d) still appears alive after SIGKILL; "
                "aborting replacement to avoid a duplicate gateway.", existing_pid)
            _clear_takeover_marker_quiet()
            return False
    # Reap orphaned children (POSIX; mirrors Windows taskkill /T) so they stop holding scoped token locks.
    try:
        from gateway.status import reap_gateway_children
        reap_gateway_children(_old_gateway_children, parent_pid=existing_pid)
    except Exception:
        logger.debug("Child reap for replaced gateway PID %d failed", existing_pid, exc_info=True)
    remove_pid_file()
    # remove_pid_file() is a no-op when the PID doesn't match; force-unlink covers a crashed old process.
    with suppress(Exception):
        (get_hermes_home() / "gateway.pid").unlink(missing_ok=True)
    # The old process may not have consumed the marker (SIGKILL'd before its handler read it).
    _clear_takeover_marker_quiet()
    # Stopped (Ctrl+Z) processes don't release scoped locks on exit; stale lock files block the new gateway.
    try:
        from gateway.status import release_all_scoped_locks
        _released = release_all_scoped_locks(owner_pid=existing_pid, owner_start_time=existing_start_time)
        if _released:
            logger.info("Released %d stale scoped lock(s) from old gateway.", _released)
    except Exception:
        pass
    return True


def _start_gateway_configure_logging(verbosity: Optional[int]) -> None:
    """Sync bundled skills, set up file logging + startup security audit, and the -v/-q stderr handler."""
    from gateway.run import (_best_effort, _gateway_stderr_formatter, _hermes_home, logging)
    def _sync_skills() -> None:
        from tools.skills_sync import sync_skills
        sync_skills(quiet=True)

    _best_effort(_sync_skills)

    # Centralized logging (agent.log INFO+, errors.log WARNING+, gateway.log gateway-only); idempotent.
    from hermes_logging import setup_logging, _safe_stderr
    setup_logging(hermes_home=_hermes_home, mode="gateway")

    def _security_audit() -> None:
        # Warn-on-load, never blocks: surfaces root / weak-SSH / unauthenticated-listener exposure.
        from hermes_cli.security_audit_startup import log_startup_security_warnings

        def _raw_cfg():
            from hermes_cli.config import read_raw_config
            return read_raw_config()

        log_startup_security_warnings(hermes_home=_hermes_home, config=_best_effort(_raw_cfg))

    _best_effort(_security_audit, "Startup security audit failed (non-fatal): %s")

    # Optional stderr handler from -v/-q: None (quiet) = none; 0 = WARNING; 1 = INFO; 2+ = DEBUG.
    if verbosity is not None:
        _stderr_level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
        _stderr_handler = logging.StreamHandler(_safe_stderr())
        _stderr_handler.setLevel(_stderr_level)
        _stderr_handler.setFormatter(_gateway_stderr_formatter())
        root = logging.getLogger()
        root.addHandler(_stderr_handler)
        if _stderr_level < root.level:  # so DEBUG records can reach the handler
            root.setLevel(_stderr_level)


def _start_gateway_make_shutdown_signal_handler(runner, _signal_initiated_shutdown: list):
    """Build the SIGINT/SIGTERM handler; ``_signal_initiated_shutdown[0]`` records an unplanned signal."""
    from gateway.run import (_best_effort, _hermes_home, asyncio, logger, signal)
    def shutdown_signal_handler(received_signal=None):
        # Planned --replace takeover (sibling marked this PID): exit 0 so systemd won't revive us.
        def _takeover() -> bool:
            from gateway.status import consume_takeover_marker_for_self
            return consume_takeover_marker_for_self()

        # Planned stop: CLI marks first, else its SIGTERM looks like an external kill. SIGINT = Ctrl+C.
        def _planned_stop() -> bool:
            from gateway.status import consume_planned_stop_marker_for_self
            return consume_planned_stop_marker_for_self()

        # Fast (<10ms) sync snapshot: stdlib + /proc, no subprocesses (`ps aux` here once blocked ~3s).
        def _snapshot():
            from gateway.shutdown_forensics import snapshot_shutdown_context
            return snapshot_shutdown_context(received_signal)

        planned_takeover = bool(_best_effort(_takeover, "Takeover marker check failed: %s"))
        planned_stop = received_signal == signal.SIGINT or (
            not planned_takeover and bool(_best_effort(_planned_stop, "Planned stop marker check failed: %s")))
        _shutdown_ctx = _best_effort(_snapshot, "snapshot_shutdown_context failed: %s")
        sig_name = _shutdown_ctx["signal"] if _shutdown_ctx else None

        if planned_takeover:
            logger.info("Received %s as a planned --replace takeover — exiting cleanly", sig_name or "SIGTERM")
        elif planned_stop:
            logger.info("Received %s as a planned gateway stop — exiting cleanly", sig_name or "SIGTERM/SIGINT")
        else:
            # Mirrored onto the runner so _stop_impl suppresses the gateway_state=stopped persist for
            # unexpected signals; operator stops take the `planned_stop` branch and leave it False (DO persist).
            _signal_initiated_shutdown[0] = runner._signal_initiated_shutdown = True
            logger.info("Received %s — initiating shutdown", sig_name or "SIGTERM/SIGINT")

        if _shutdown_ctx is not None:
            def _log_context() -> None:
                # The most useful line for "gateway keeps dying" tickets.
                from gateway.shutdown_forensics import format_context_for_log
                logger.warning("Shutdown context: %s", format_context_for_log(_shutdown_ctx))

            def _diagnostic() -> None:
                # Heavyweight (ps auxf, pstree, dmesg), detached so it finishes even if our cgroup is torn
                # down; bounded by an internal timeout, never blocks.
                from gateway.shutdown_forensics import spawn_async_diagnostic
                spawn_async_diagnostic(
                    _hermes_home / "logs" / "gateway-shutdown-diag.log", _shutdown_ctx["signal"], timeout_seconds=5.0)

            _best_effort(_log_context, "format_context_for_log failed: %s")
            _best_effort(_diagnostic, "spawn_async_diagnostic failed: %s")
        asyncio.create_task(runner.stop())
    return shutdown_signal_handler


def _start_gateway_claim_pid_file() -> bool:
    """Claim the runtime lock + PID file (O_EXCL winner is the authoritative gateway). False = lost."""
    from gateway.run import (logger, os)
    import atexit
    from gateway.status import (
        acquire_gateway_runtime_lock, get_running_pid, release_gateway_runtime_lock,
        remove_pid_file, write_pid_file)
    _current_pid = get_running_pid()
    if _current_pid is not None and _current_pid != os.getpid():
        logger.error("Another gateway instance (PID %d) started during our startup. "
                     "Exiting to avoid double-running.", _current_pid)
        return False
    if not acquire_gateway_runtime_lock():
        logger.error("Gateway runtime lock is already held by another instance. Exiting.")
        return False
    try:
        write_pid_file()
    except FileExistsError:
        release_gateway_runtime_lock()
        logger.error("PID file race lost to another gateway instance. Exiting.")
        return False
    atexit.register(remove_pid_file)
    atexit.register(release_gateway_runtime_lock)
    return True


async def _start_gateway_start_control_socket(runner):
    """Start the gateway control socket (identify/status/pause-for-update); None when unavailable."""
    from gateway.run import (asyncio, logger, os, threading)
    import atexit
    _control_server = None
    try:
        # Started immediately after the PID-file claim: winning that O_EXCL race is the moment this process
        # becomes the authoritative gateway for its HERMES_HOME, so from here on "does a socket answer?" is
        # a truthful liveness/identity query for updater and fleet consumers. Strictly non-fatal: a bind
        # failure only means consumers fall back to the process-scan/state-file layer, exactly as before
        # this feature. See #92091.
        from gateway.control_socket import GatewayControlServer
        # pause-for-update: the updater asks us to drain + exit (freeing venv handles) vs. a tree-kill
        # (same path as SIGUSR1). Handler runs on the socket executor thread, so marshal onto the loop.
        # pause-for-update (#92091 step 2): the updater asks this gateway to drain in-flight turns and exit
        # cleanly — releasing every venv file handle — instead of being tree-killed mid-turn. Same drain
        # path as SIGUSR1/service restarts (request_restart(via_service=True)); the updater (or the service
        # manager) relaunches after the code swap.
        _main_loop = asyncio.get_running_loop()

        def _pause_for_update_handler() -> dict:
            try:
                from hermes_cli.gateway import _get_restart_drain_timeout
                _drain = float(_get_restart_drain_timeout())
            except Exception:
                _drain = 30.0
            accepted_box: list[bool] = []
            _done = threading.Event()

            def _request() -> None:
                try:
                    accepted_box.append(runner.request_restart(detached=False, via_service=True))
                finally:
                    _done.set()

            _main_loop.call_soon_threadsafe(_request)
            _done.wait(timeout=5.0)
            accepted = bool(accepted_box and accepted_box[0])
            return {
                "pausing": accepted, "already_stopping": not accepted,
                "pid": os.getpid(), "drain_timeout": _drain}

        _control_server = GatewayControlServer(
            verb_handlers={"pause-for-update": _pause_for_update_handler})
        if not await _control_server.start():
            _control_server = None
        else:
            atexit.register(_control_server.cleanup_files)
    except Exception as _cs_exc:
        logger.debug("Control socket startup failed (non-fatal): %s", _cs_exc)
        _control_server = None
    return _control_server


def _start_gateway_start_cron_and_housekeeping(runner):
    """Start the cron scheduler thread + gateway housekeeping thread; returns
    ``(cron_stop, cron_provider, cron_thread, housekeeping_thread)``."""
    from gateway.run import (Any, Dict, Platform, _multiplex_profile_homes, _start_gateway_housekeeping, asyncio, logger, threading)
    # The event loop is passed so cron delivery can use live adapters (E2EE support).
    from cron.scheduler_provider import (
        InProcessCronScheduler, resolve_cron_scheduler, scheduler_for_profile_mode)
    cron_stop = threading.Event()
    multiplex_cron = bool(getattr(runner.config, "multiplex_profiles", False))
    cron_provider = scheduler_for_profile_mode(
        resolve_cron_scheduler(), multiplex_profiles=multiplex_cron)
    cron_start_kwargs: Dict[str, Any] = {"adapters": runner.adapters, "loop": asyncio.get_running_loop()}

    # Multiplex: tell the ticker which profile homes to tick, else secondary profiles' jobs never run.
    if isinstance(cron_provider, InProcessCronScheduler) and multiplex_cron:
        try:
            profile_homes = _multiplex_profile_homes(runner.config)
            if profile_homes:
                cron_start_kwargs["profile_homes"] = profile_homes
                # Per-profile adapters so each profile's cron output goes via its own bot, not the default's.
                cron_start_kwargs["profile_adapters"] = getattr(runner, "_profile_adapters", None)
                # runner.adapters belongs to "default"; naming it keeps the ticker from routing a secondary's
                # cron through the default bot (even before that profile's adapter connects).
                cron_start_kwargs["default_profile"] = "default"
                logger.info(
                    "Cron scheduler will tick %d profile(s) under multiplex: %s", len(profile_homes),
                    [p[0] if isinstance(p, tuple) else p for p in profile_homes])
        except Exception as exc:
            logger.warning("Could not resolve profile homes for multiplex cron: %s", exc)

    # Only the in-process ticker polls local due jobs, so only it gets the external-drain dispatch gate.
    if isinstance(cron_provider, InProcessCronScheduler):
        cron_start_kwargs["can_dispatch"] = lambda: not (
            runner._draining or runner._external_drain_active)
    cron_thread = threading.Thread(
        target=cron_provider.start, args=(cron_stop,), kwargs=cron_start_kwargs, daemon=True,
        name="cron-scheduler")
    cron_thread.start()

    # External providers fire over loopback HTTP to THIS process's api_server; if it never came up (usually
    # API_SERVER_KEY missing) every fire fails while manual runs work — misread as a job bug. Say it ONCE.
    if not isinstance(cron_provider, InProcessCronScheduler):
        try:
            _has_api_server = Platform.API_SERVER in (runner.adapters or {})
        except Exception:
            _has_api_server = True  # never let the tell break startup
        if not _has_api_server:
            logger.warning(
                "Cron provider '%s' is active but the api_server adapter is "
                "NOT running in this gateway — scheduled fires arrive over "
                "loopback HTTP and will all fail (jobs only run when "
                "triggered manually). Most common cause: API_SERVER_KEY is "
                "missing from this gateway process's environment. Restart "
                "the gateway through its supervisor (`hermes gateway "
                "restart`) so the profile env loads.",
                getattr(cron_provider, "name", "external"))

    # Gateway-only housekeeping runs independently of the cron provider; shares cron_stop for shutdown.
    housekeeping_thread = threading.Thread(
        target=_start_gateway_housekeeping, args=(cron_stop,),
        kwargs={"adapters": runner.adapters, "loop": asyncio.get_running_loop(),
                "cron_provider": cron_provider, "runner": runner},
        daemon=True, name="gateway-housekeeping")
    housekeeping_thread.start()
    return cron_stop, cron_provider, cron_thread, housekeeping_thread


async def _start_gateway_shutdown_tail(
    runner, _control_server, cron_stop: threading.Event, cron_provider,
    cron_thread: threading.Thread, housekeeping_thread: threading.Thread,
    _planned_stop_watcher_stop: threading.Event, _planned_stop_watcher_thread: threading.Thread,
    _signal_initiated_shutdown: list) -> bool:
    """Post-``wait_for_shutdown`` teardown; returns the process exit verdict (True = exit 0)."""
    from gateway.run import (_CRON_SHUTDOWN_DRAIN_TIMEOUT, _HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT, _await_thread_exit, _best_effort, _exit_with_failure_verdict, _resolve_gateway_exit_verdict, _shutdown_mcp_servers_nonblocking, _stop_cron_provider, logger, suppress)
    # Control socket first: once shutdown begins we are no longer a truthful "serving here" answer and a
    # successor must be able to bind. Early-exit paths rely on the atexit cleanup_files hook instead.
    if _control_server is not None:
        try:
            await _control_server.stop()
        except Exception:
            logger.debug("Control socket stop failed (non-fatal)", exc_info=True)

    def _stop_keepalive() -> None:
        from hermes_cli.nous_auth_keepalive import stop_nous_auth_keepalive
        stop_nous_auth_keepalive()

    _best_effort(_stop_keepalive)
    if _exit_with_failure_verdict(runner):
        return False

    # Never join(): an in-flight cron delivery is a coroutine on THIS loop; a sync join would drop it.
    # Stop cron scheduler + housekeeping cleanly. These MUST be awaited cooperatively, not join()ed. A cron
    # delivery in flight when the gateway restarts is a coroutine scheduled onto THIS event loop
    # (safe_schedule_threadsafe); the ticker thread is blocked on its future.result(). A synchronous
    # cron_thread.join() would block the loop, so that delivery could never run — it timed out and the
    # message was silently dropped (#58818). Awaiting keeps the loop alive so the in-flight delivery
    # finishes before we tear down.
    cron_stop.set()
    _stop_cron_provider(cron_provider)
    if not await _await_thread_exit(cron_thread, timeout=_CRON_SHUTDOWN_DRAIN_TIMEOUT):
        logger.warning("Cron ticker did not exit within %.0fs of shutdown — an in-flight "
                       "delivery may have been dropped.", _CRON_SHUTDOWN_DRAIN_TIMEOUT)
    await _await_thread_exit(housekeeping_thread, timeout=_HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT)

    # Stop the planned-stop watcher (daemon=True so this is belt-and-suspenders).
    _planned_stop_watcher_stop.set()
    _planned_stop_watcher_thread.join(timeout=2)

    with suppress(Exception):
        await _shutdown_mcp_servers_nonblocking()

    return _resolve_gateway_exit_verdict(runner, _signal_initiated_shutdown[0])


