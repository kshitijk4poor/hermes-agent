"""Content-free gauges for how the previous gateway life ended and live host memory pressure.

Read from the gateway's own home only (the lifecycle sentinel and the 30 s loop heartbeat, via
``gateway.memory_status.collect_memory_status``): two small host-file reads, no probes.  Nothing
is emitted when the watchdog fires: the process hard-exits right after and the emitter is
asynchronous, so the record would never leave; the next boot's ``previous_exit`` gauge carries it.
"""

from __future__ import annotations

from typing import Any

from agent.monitoring.gateway_health import GatewayMetric

PREVIOUS_EXIT_METRIC = "hermes.gateway.previous_exit"
MEMORY_PRESSURE_METRIC = "hermes.gateway.memory_pressure"
_PRESSURE_LEVELS = frozenset({"ok", "elevated", "critical", "unknown"})
# Worst-first: the first matching ``collect_memory_status`` flag names the previous exit.
_PREVIOUS_EXIT_PRECEDENCE = (
    ("last_boot_oom_killed", "oom_killed"),
    ("last_boot_memory_starved", "watchdog_memory_starved"),
    ("last_boot_watchdog_exit", "watchdog"),
    ("last_boot_suspected_oom", "suspected_oom"),
    ("last_boot_unclean", "unclean"),
)


def previous_exit_class(status: dict[str, Any]) -> str:
    """Closed-set label for the previous life: one of the precedence labels above, ``clean``
    when this life has a sentinel claim and no flag is set, else ``unknown``."""
    for flag, label in _PREVIOUS_EXIT_PRECEDENCE:
        if status.get(flag) is True:
            return label
    return "clean" if status.get("boot_id") else "unknown"


def build_memory_health_metrics(base: dict[str, str]) -> list[GatewayMetric]:
    """``previous_exit`` and ``memory_pressure`` gauges (value 1, bounded attribute) carrying
    the snapshot's ``base`` attributes."""
    from gateway.memory_status import collect_memory_status

    status = collect_memory_status()
    pressure = status.get("pressure")
    return [
        GatewayMetric(name=PREVIOUS_EXIT_METRIC, value=1,
                      attributes={**base, "hermes.previous_exit": previous_exit_class(status)}),
        GatewayMetric(name=MEMORY_PRESSURE_METRIC, value=1,
                      attributes={**base, "hermes.memory_pressure": pressure if pressure in _PRESSURE_LEVELS else "unknown"}),
    ]
