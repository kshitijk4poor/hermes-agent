"""Gateway event-loop freeze backstops for issue #69089."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import threading
import time
from unittest.mock import MagicMock, patch

from gateway.shutdown_watchdog import (
    loop_heartbeat_forever,
    start_loop_liveness_watchdog,
)

def test_loop_liveness_watchdog_stop_during_dump_disarms_hard_exit():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    handle_ready = threading.Event()
    handle_ref = {}
    exit_codes = []

    def stop_during_dump(*_args, **_kwargs) -> None:
        assert handle_ready.wait(timeout=2.0)
        handle_ref["handle"].stop()

    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch(
            "gateway.shutdown_watchdog.faulthandler.dump_traceback",
            side_effect=stop_during_dump,
        ) as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        handle_ref["handle"] = handle
        handle_ready.set()
        handle.join(timeout=2.0)

    assert not handle.is_alive()
    critical.assert_called_once()
    dump.assert_called()  # dump-file copy first, then stderr (#120356)
    assert exit_codes == []


def test_loop_liveness_death_leaves_its_stacks_in_the_dump_file(tmp_path, monkeypatch):
    # A console-less launcher discards stderr; the file is the only copy that survives (#120356).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    exit_codes, ref, handle_ready = [], {}, threading.Event()

    def exit_and_stop(code: int) -> None:  # a patched exit returns, so stop the loop like a real one
        assert handle_ready.wait(timeout=2.0)
        exit_codes.append(code)
        ref["handle"].stop()

    with patch("gateway.shutdown_watchdog._hard_exit", side_effect=exit_and_stop), \
            patch("gateway.shutdown_watchdog._mark_exited_quietly"):
        ref["handle"] = handle = start_loop_liveness_watchdog(
            MagicMock(spec=asyncio.AbstractEventLoop), probe_interval=0.01, probe_timeout=0.01,
            max_strikes=1)
        handle_ready.set()
        handle.join(timeout=5.0)
    from gateway.shutdown_watchdog import get_shutdown_watchdog_dump_path
    dump = get_shutdown_watchdog_dump_path(tmp_path).read_text(encoding="utf-8-sig")
    assert '"event": "loop_liveness_watchdog_fired"' in dump
    assert "most recent call first" in dump
    assert exit_codes == [75]


def test_loop_liveness_watchdog_stop_during_final_miss_disarms_hard_exit():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    probe_scheduled = threading.Event()
    release_probe = threading.Event()
    probe_event_ref = {}
    handle_ref = {}
    exit_codes = []

    class FinalStrikeLimit:
        def __gt__(self, _strikes: int) -> bool:
            # If strike evaluation is reached, keep recheck #2 from masking a
            # missing post-probe recheck #1 in this boundary test.
            handle_ref["handle"]._stop_event.clear()
            return False

    def hold_scheduled_probe(callback) -> None:
        probe_event_ref["event"] = callback.__self__
        probe_scheduled.set()
        assert release_probe.wait(timeout=2.0)

    loop.call_soon_threadsafe.side_effect = hold_scheduled_probe
    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit", side_effect=exit_codes.append),
    ):
        handle = start_loop_liveness_watchdog(
            loop,
            probe_interval=0.01,
            probe_timeout=0.01,
            max_strikes=FinalStrikeLimit(),
        )
        assert handle is not None
        handle_ref["handle"] = handle
        assert probe_scheduled.wait(timeout=2.0), "watchdog did not schedule a probe"

        def stop_during_miss() -> bool:
            handle.stop()
            return False

        probe_event_ref["event"].is_set = stop_during_miss
        release_probe.set()
        handle.join(timeout=1.0)

    assert not handle.is_alive()
    assert exit_codes == []
    critical.assert_not_called()
    dump.assert_not_called()

def test_loop_liveness_watchdog_stop_after_first_recheck_skips_final_actions():
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    probe_scheduled = threading.Event()
    release_probe = threading.Event()

    def hold_scheduled_probe(callback) -> None:
        probe_scheduled.set()
        assert release_probe.wait(timeout=2.0)

    loop.call_soon_threadsafe.side_effect = hold_scheduled_probe
    with (
        patch("gateway.shutdown_watchdog.logger.critical") as critical,
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback") as dump,
        patch("gateway.shutdown_watchdog.os._exit") as hard_exit,
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
        )
        assert handle is not None
        assert probe_scheduled.wait(timeout=2.0), "watchdog did not schedule a probe"

        original_is_set = handle._stop_event.is_set
        is_set_calls = 0

        def stop_on_final_recheck() -> bool:
            nonlocal is_set_calls
            is_set_calls += 1
            # With the forced immediate timeout: _wait_for_probe is call 1,
            # recheck #1 is call 2, and recheck #2 is call 3.
            if is_set_calls == 3:
                handle.stop()
            return original_is_set()

        handle._stop_event.is_set = stop_on_final_recheck
        with patch(
            "gateway.shutdown_watchdog.time.monotonic", side_effect=[0.0, 1.0]
        ):
            release_probe.set()
            handle.join(timeout=1.0)

    assert is_set_calls == 3
    assert not handle.is_alive()
    critical.assert_not_called()
    dump.assert_not_called()
    hard_exit.assert_not_called()

def test_gateway_config_loop_watchdog_round_trip():
    """loop_watchdog is a config.yaml knob: default on, nested-gateway form honored."""
    from gateway.config import GatewayConfig

    assert GatewayConfig.from_dict({}).loop_watchdog is True
    assert GatewayConfig.from_dict({"loop_watchdog": False}).loop_watchdog is False
    assert (
        GatewayConfig.from_dict(
            {"gateway": {"loop_watchdog": "off"}}
        ).loop_watchdog
        is False
    )
    config = GatewayConfig.from_dict({"loop_watchdog": False})
    assert config.to_dict()["loop_watchdog"] is False

def test_gateway_config_loop_watchdog_tuning_round_trip():
    """Watchdog tolerance knobs parse, serialize, and clamp malformed values."""
    from gateway.config import GatewayConfig

    # Defaults
    default = GatewayConfig.from_dict({})
    assert default.loop_watchdog is True
    assert default.loop_watchdog_probe_interval_s == 30.0
    assert default.loop_watchdog_probe_timeout_s == 10.0
    assert default.loop_watchdog_max_strikes == 3

    # Explicit values round-trip
    cfg = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": 45,
            "loop_watchdog_probe_timeout_s": 15,
            "loop_watchdog_max_strikes": 12,
        }
    )
    assert cfg.loop_watchdog_probe_interval_s == 45.0
    assert cfg.loop_watchdog_probe_timeout_s == 15.0
    assert cfg.loop_watchdog_max_strikes == 12
    d = cfg.to_dict()
    assert d["loop_watchdog_probe_interval_s"] == 45.0
    assert d["loop_watchdog_probe_timeout_s"] == 15.0
    assert d["loop_watchdog_max_strikes"] == 12

    # Nested gateway.* form honored
    nested = GatewayConfig.from_dict(
        {
            "gateway": {
                "loop_watchdog_probe_interval_s": 60,
                "loop_watchdog_probe_timeout_s": 20,
                "loop_watchdog_max_strikes": 20,
            }
        }
    )
    assert nested.loop_watchdog_probe_interval_s == 60.0
    assert nested.loop_watchdog_probe_timeout_s == 20.0
    assert nested.loop_watchdog_max_strikes == 20

    # Malformed / degenerate values fall back to safe defaults
    clamped = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": 0,
            "loop_watchdog_probe_timeout_s": -5,
            "loop_watchdog_max_strikes": 0,
        }
    )
    assert clamped.loop_watchdog_probe_interval_s == 30.0
    assert clamped.loop_watchdog_probe_timeout_s == 10.0
    assert clamped.loop_watchdog_max_strikes == 3

def test_gateway_config_loop_watchdog_nonfinite_values_degrade():
    """NaN/Inf tuning values fall back to defaults instead of reaching the
    watchdog's Event.wait loop (or aborting config load via int(inf))."""
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": float("inf"),
            "loop_watchdog_probe_timeout_s": float("nan"),
            "loop_watchdog_max_strikes": float("inf"),  # int() would raise
        }
    )
    assert cfg.loop_watchdog_probe_interval_s == 30.0
    assert cfg.loop_watchdog_probe_timeout_s == 10.0
    assert cfg.loop_watchdog_max_strikes == 3

    # Oversized-but-finite values also clamp to defaults.
    big = GatewayConfig.from_dict(
        {
            "loop_watchdog_probe_interval_s": 86400,
            "loop_watchdog_probe_timeout_s": 7200,
            "loop_watchdog_max_strikes": 10**9,
        }
    )
    assert big.loop_watchdog_probe_interval_s == 30.0
    assert big.loop_watchdog_probe_timeout_s == 10.0
    assert big.loop_watchdog_max_strikes == 3

def test_load_gateway_config_bridges_loop_watchdog_keys(tmp_path, monkeypatch):
    """The real startup loader must honor gateway.loop_watchdog* from
    config.yaml — from_dict's nested fallback never sees the yaml gateway
    section because load_gateway_config builds gw_data flat."""
    from gateway.config import load_gateway_config

    (tmp_path / "config.yaml").write_text(
        "gateway:\n"
        "  loop_watchdog: false\n"
        "  loop_watchdog_probe_interval_s: 45\n"
        "  loop_watchdog_probe_timeout_s: 15\n"
        "  loop_watchdog_max_strikes: 12\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.config.get_hermes_home", lambda: tmp_path)

    cfg = load_gateway_config()
    assert cfg.loop_watchdog is False
    assert cfg.loop_watchdog_probe_interval_s == 45.0
    assert cfg.loop_watchdog_probe_timeout_s == 15.0
    assert cfg.loop_watchdog_max_strikes == 12

def test_loop_liveness_watchdog_marks_runtime_degraded_before_restart():
    """The terminal watchdog observation must be visible before ``os._exit``."""
    from gateway.status import read_runtime_status, write_runtime_status

    write_runtime_status(gateway_state="running", exit_reason=None)
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    fired = threading.Event()
    exit_codes = []

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        fired.set()

    with (
        patch("gateway.shutdown_watchdog.faulthandler.dump_traceback"),
        patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit),
    ):
        handle = start_loop_liveness_watchdog(
            loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=2
        )
        assert handle is not None
        assert fired.wait(timeout=2.0), "watchdog did not reach its restart exit"
        handle.stop()
        handle.join(timeout=1.0)

    record = read_runtime_status()
    assert exit_codes == [75]
    assert record["gateway_state"] == "degraded"
    assert record["exit_reason"] == "loop_liveness_watchdog"
    assert record["restart_requested"] is True

def test_heartbeat_write_does_not_block_the_loop_it_monitors():
    """The heartbeat write must not freeze the loop the watchdog is watching.

    ``write_loop_heartbeat`` ends in ``atomic_json_write`` -> ``os.fsync``, and on
    a stalling filesystem that fsync blocks whichever thread runs it. Run inline,
    that thread was the gateway loop — so the loop-liveness watchdog would time
    out its probe (10s, 3 strikes, a ~90-120s budget) and kill the loop for being
    unresponsive at the moment it was blocked inside the watchdog's own write. A
    WSL2 VHDX under io pressure was measured stalling a trivial stat-and-fsync
    probe at p99 31s, max 112s — longer than the whole budget.

    Bounded by a fixed sleep rather than an Event handshake on purpose: if the
    write ever goes back on-loop this fails on the tick count instead of hanging.
    """
    block_s = 0.30

    def slow_write(**_kwargs):
        time.sleep(block_s)
        return pathlib.Path("/dev/null")

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            deadline = time.monotonic() + block_s
            while time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                ticks += 1

        with patch(
            "gateway.shutdown_watchdog.write_loop_heartbeat", slow_write
        ):
            # should_continue False -> exactly one write, then return.
            hb = asyncio.create_task(
                loop_heartbeat_forever(interval_s=60.0, should_continue=lambda: False)
            )
            await ticker()
            await asyncio.wait_for(hb, timeout=5.0)
        return ticks

    ticks = asyncio.run(scenario())
    # Off-loop, the ticker gets roughly block_s / 0.02 ticks. Inline it gets at
    # most one, because the loop cannot run anything while fsync blocks it.
    assert ticks >= 5, (
        f'the loop made only {ticks:d} tick(s) while the heartbeat was writing — the write is blocking the loop again'
    )



# --- Fire-time host-memory forensics (#148527) -------------------------------------------------

_PSI_MEMORY = ("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
               "full avg10=41.00 avg60=30.12 avg300=10.00 total=123\n")
_PSI_IO = ("some avg10=90.00 avg60=80.00 avg300=50.00 total=999\n"
           "full avg10=83.00 avg60=70.50 avg300=40.00 total=888\n")
_CRITICAL_MEM = {"rss_kib": 300 * 1024, "mem_total_kib": 2048 * 1024,
                 "mem_available_kib": 40 * 1024, "swap_used_kib": 0}
_STARVATION_LINE = "Host memory pressure is critical"


def _fire_liveness_watchdog(tmp_path, monkeypatch, sample_memory) -> list[int]:
    """Run the real watchdog thread to its (patched) hard exit with real tmp PSI files and a
    sentinel claimed for this process; return the exit codes it asked for."""
    from gateway.lifecycle_ledger import record_startup

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    psi_root = tmp_path / "pressure"
    psi_root.mkdir()
    (psi_root / "memory").write_text(_PSI_MEMORY, encoding="utf-8")
    (psi_root / "io").write_text(_PSI_IO, encoding="utf-8")
    monkeypatch.setattr("gateway.lifecycle_ledger._PSI_ROOT", psi_root)
    monkeypatch.setattr("gateway.shutdown_watchdog.sample_memory", sample_memory)
    record_startup(tmp_path)
    exit_codes, ref, handle_ready = [], {}, threading.Event()

    def exit_and_stop(code: int) -> None:  # a patched exit returns, so stop the loop like a real one
        assert handle_ready.wait(timeout=5.0)
        exit_codes.append(code)
        ref["handle"].stop()

    with patch("gateway.shutdown_watchdog._hard_exit", side_effect=exit_and_stop):
        ref["handle"] = handle = start_loop_liveness_watchdog(
            MagicMock(spec=asyncio.AbstractEventLoop), probe_interval=0.01, probe_timeout=0.01,
            max_strikes=1)
        assert handle is not None
        handle_ready.set()
        handle.join(timeout=5.0)
    assert not handle.is_alive()
    return exit_codes


def _dump_snapshot(home) -> dict:
    from gateway.shutdown_watchdog import get_shutdown_watchdog_dump_path
    header = get_shutdown_watchdog_dump_path(home).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(header)["snapshot"]


def _sentinel(home) -> dict:
    from gateway.lifecycle_ledger import get_lifecycle_sentinel_path
    return json.loads(get_lifecycle_sentinel_path(home).read_text(encoding="utf-8"))


def _starvation_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if _STARVATION_LINE in r.getMessage()]


def test_liveness_kill_on_starved_host_records_memory_and_says_why(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="gateway.shutdown_watchdog")
    exit_codes = _fire_liveness_watchdog(tmp_path, monkeypatch, lambda: dict(_CRITICAL_MEM))

    assert exit_codes == [75]
    snapshot = _dump_snapshot(tmp_path)
    assert snapshot["strikes"] == 1
    assert snapshot["host_memory_pressure"] == "critical"
    assert snapshot["mem"]["mem_available_kib"] == 40 * 1024
    assert snapshot["psi"]["memory"] == {"some_avg10": 0.0, "some_avg60": 0.0,
                                         "full_avg10": 41.0, "full_avg60": 30.12}
    assert snapshot["psi"]["io"]["full_avg10"] == 83.0
    sentinel = _sentinel(tmp_path)
    assert sentinel["exit_reason"] == "loop_liveness_watchdog"
    assert sentinel["host_memory_pressure"] == "critical"
    [line] = _starvation_lines(caplog)
    for fragment in ("MemAvailable 40 MiB of 2048 MiB", "gateway RSS 300 MiB", "full avg10=41.0",
                     "frees only the gateway's own memory"):
        assert fragment in line


def test_starvation_kill_surfaces_as_memory_starved_on_next_boot(tmp_path, monkeypatch):
    from gateway.lifecycle_ledger import record_startup
    from gateway.memory_status import collect_memory_status

    _fire_liveness_watchdog(tmp_path, monkeypatch, lambda: dict(_CRITICAL_MEM))
    record_startup(tmp_path)  # the supervisor's restart

    status = collect_memory_status(tmp_path)
    assert status["last_boot_watchdog_exit"] is True
    assert status["last_boot_memory_starved"] is True
    assert status["last_boot_unclean"] is False  # a watchdog exit is not an unclean death


def _assert_quiet_kill(tmp_path, monkeypatch, caplog, sample: dict, expected: str) -> None:
    caplog.set_level(logging.WARNING, logger="gateway.shutdown_watchdog")
    exit_codes = _fire_liveness_watchdog(tmp_path, monkeypatch, lambda: dict(sample))

    assert exit_codes == [75]
    assert _dump_snapshot(tmp_path)["host_memory_pressure"] == expected
    assert _sentinel(tmp_path)["host_memory_pressure"] == expected
    assert _starvation_lines(caplog) == []


def test_liveness_kill_with_plenty_of_memory_logs_no_starvation(tmp_path, monkeypatch, caplog):
    _assert_quiet_kill(tmp_path, monkeypatch, caplog, {
        "rss_kib": 300 * 1024, "mem_total_kib": 2048 * 1024, "mem_available_kib": 1536 * 1024}, "ok")


def test_liveness_kill_without_a_memory_sample_logs_no_starvation(tmp_path, monkeypatch, caplog):
    _assert_quiet_kill(tmp_path, monkeypatch, caplog, {}, "unknown")  # sample_memory off Linux


def test_liveness_kill_survives_a_failing_memory_sampler(tmp_path, monkeypatch):
    def broken_sampler() -> dict:
        raise RuntimeError("procfs exploded")

    exit_codes = _fire_liveness_watchdog(tmp_path, monkeypatch, broken_sampler)

    assert exit_codes == [75]  # forensics must never block the restart
    snapshot = _dump_snapshot(tmp_path)
    assert snapshot["strikes"] == 1
    assert "host_memory_pressure" not in snapshot
    sentinel = _sentinel(tmp_path)
    assert sentinel["exit_reason"] == "loop_liveness_watchdog"
    assert "host_memory_pressure" not in sentinel
