"""``hermes cron tick`` with no live gateway refuses agent jobs instead of spawning one.

The external-scheduler mode (system crontab calling ``hermes cron tick``) used to reach
``run_canonical_job`` -> ``connect_gateway`` -> ``ensure_gateway_runtime``, which spawns an
unmanaged daemon when nothing owns the profile. Headless surfaces refuse rather than spawn:
the agent job is skipped (not failed), keeps its due instant, and one warning names the fix.
"""
from datetime import timedelta

import pytest

import cron.jobs as J
import cron.scheduler as S
from cron import scheduler_authority


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(S, "_hermes_home", home)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    S._running_job_ids.clear()
    return home


def _make_due(job_id: str) -> None:
    stored = J.load_jobs()
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    next(r for r in stored if r["id"] == job_id)["next_run_at"] = slot
    J.save_jobs(stored)


def test_tick_without_gateway_skips_agent_jobs_without_spawning_or_drift(cron_home, monkeypatch, caplog):
    from hermes_cli import gateway_runtime, gateway_runtime_start

    spawned = []
    monkeypatch.setattr(gateway_runtime_start, "spawn_unmanaged_gateway",
                        lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(gateway_runtime, "ensure_gateway_runtime",
                        lambda *a, **k: pytest.fail("a headless tick must never ensure-or-spawn"))

    agent = J.create_job(prompt="summarise inbox", schedule="every 1h", name="agent", deliver="local")
    other = J.create_job(prompt="digest", schedule="every 1h", name="agent-2", deliver="local")
    _make_due(agent["id"])
    _make_due(other["id"])
    due_before = {j["id"]: j["next_run_at"] for j in J.load_jobs()}

    with caplog.at_level("WARNING", logger="cron.scheduler_authority"):
        ran = S.tick(verbose=False, headless=True)

    assert ran == 0 and spawned == []
    stored = {j["id"]: j for j in J.load_jobs()}
    for job_id in (agent["id"], other["id"]):
        # Skipped, not failed: no run recorded, the due instant is untouched (no drift), no
        # orphaned dispatch stamp for the next tick to "restore", and the reason is visible.
        assert stored[job_id]["last_run_at"] is None
        assert stored[job_id]["next_run_at"] == due_before[job_id]
        assert "pending_slot" not in stored[job_id]
        assert "hermes gateway start" in stored[job_id]["last_fire_error"]["detail"]
    refusals = [r for r in caplog.records if "not running" in r.getMessage()]
    assert len(refusals) == 1, "one log line per tick, not one per job"
    assert agent["id"] in refusals[0].getMessage() and "hermes gateway start" in refusals[0].getMessage()


def test_no_agent_jobs_still_fire_without_a_gateway(cron_home, monkeypatch):
    counter = cron_home / "fires.txt"
    script = cron_home / "scripts" / "fire.sh"
    script.write_text(f"#!/bin/sh\necho fired >> {counter}\necho fired\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", lambda: None)
    job = J.create_job(prompt=None, schedule="every 1h", name="script", script="fire.sh",
                       no_agent=True, deliver="local")
    _make_due(job["id"])

    assert S.tick(verbose=False, headless=True) == 1
    assert counter.read_text(encoding="utf-8").count("fired") == 1
