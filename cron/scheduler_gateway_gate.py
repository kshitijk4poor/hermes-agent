"""Headless tick gate: agent jobs need a live gateway; a headless tick never starts one.

``run_canonical_job`` reaches the owner through ``connect_gateway`` → ``ensure_gateway_runtime``,
which spawns an unmanaged daemon when nothing owns the profile. That is right for an interactive
caller (``hermes cron run``) and wrong for ``hermes cron tick`` from a system crontab: every tick
would leave a stray gateway behind. Headless surfaces refuse rather than spawn (same policy as
approvals), so a headless tick skips its agent jobs while the gateway is down and lets them fire
on the next tick once it is up — no run is recorded and the due instant does not move. The
in-process ticker never reaches this gate: it runs inside the gateway by definition.
"""
import logging
import os

logger = logging.getLogger(__name__)

GATEWAY_DOWN_HINT = (
    "the gateway is not running, so agent jobs cannot fire; they run at the next tick once it is "
    "up. Start it with `hermes gateway start` (or `hermes gateway install` for a service)."
)


def _gateway_absent(home) -> bool:
    """True in exactly the state where ``ensure_gateway_runtime`` would spawn a daemon. Every
    other state (starting, ready, draining, incompatible, ...) is left to ``connect_gateway``,
    which awaits or reports it without spawning."""
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    if os.environ.get("HERMES_TUI_GATEWAY_URL", "").strip():
        return False  # explicit remote gateway: connect_gateway never spawns for it
    return discover_gateway_endpoint(home).state == "absent"


def _note_fire_refused(job_ids, detail: str) -> None:
    """Stamp ``last_fire_error`` (shown by ``hermes cron list``) and drop the dispatch marks the
    due scan just wrote: the slot was never handed over, so nothing must "restore" or "reclaim" it."""
    from cron.jobs import _hermes_now, _jobs_lock, load_jobs, save_jobs

    ids = set(job_ids)
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job["id"] not in ids:
                continue
            job["last_fire_error"] = {"at": _hermes_now().isoformat(), "detail": detail[:500]}
            job.pop("pending_slot", None)
            if job.get("run_claim") is not None:
                job["run_claim"] = None
        save_jobs(jobs)


def refuse_agent_jobs_without_gateway(due_jobs: list) -> list:
    """Return the due jobs a headless tick may dispatch; agent jobs are held back (skipped, not
    failed) when no gateway serves this home. Logs once per tick, not once per job."""
    from hermes_constants import get_hermes_home

    agent_jobs = [job for job in due_jobs if not job.get("no_agent")]
    if not agent_jobs:
        return due_jobs
    home = get_hermes_home().resolve()
    if not _gateway_absent(home):
        return due_jobs
    ids = [str(job["id"]) for job in agent_jobs]
    _note_fire_refused(ids, f"Skipped: {GATEWAY_DOWN_HINT}")
    logger.warning(
        "Cron tick for %s skipped %d agent job(s) (%s): %s",
        home, len(ids), ", ".join(ids), GATEWAY_DOWN_HINT)
    return [job for job in due_jobs if job.get("no_agent")]
