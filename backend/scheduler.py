"""Lightweight cron scheduler for Beacon.

Beacon persists cron jobs as YAML in fleet/cron_jobs/ but does *not* run a
long-lived scheduler process. Instead, an external minute-level trigger
(system cron, launchd, k8s CronJob, etc.) calls
`python -m backend.scheduler tick`, which evaluates every enabled job and
dispatches the ones that are due.

This keeps scheduling separate from the web server, avoids adding a
background-thread/asyncio complexity to FastAPI, and keeps the same
YAML-backed, git-diffable fleet model.
"""

import argparse
import datetime
import sys
from collections.abc import Iterator

import yaml

from . import cron_store, store
from .drivers import get_driver
from .schemas import CronJob

SUPPORTED_ACTIONS = {"restart", "push_config", "status"}

# For last-run timestamps.
_UTC = datetime.timezone.utc

# 5-field cron: minute hour day-of-month month day-of-week
_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def _field_matches(field: str, value: int, min_val: int, max_val: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return (value - min_val) % step == 0
    if "," in field:
        return any(_field_matches(part, value, min_val, max_val) for part in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return int(field) == value


def _is_due(schedule: str, dt: datetime.datetime) -> bool:
    """Evaluate a 5-field cron expression against `dt` (naive, in the caller's
    timezone). We use the caller-provided `dt` rather than converting timezones
    so the scheduler can be run with a consistent UTC or local clock.
    """
    fields = schedule.split()
    values = [dt.minute, dt.hour, dt.day, dt.month, dt.isoweekday() % 7]
    for field, value, (min_val, max_val) in zip(fields, values, _CRON_RANGES):
        if not _field_matches(field, value, min_val, max_val):
            return False
    return True


def _run_action(job_id: str, agent_id: str, action: str, timeout: int | None) -> dict:
    agent = store.get_agent(agent_id, resolved=True)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    if action == "restart":
        return driver.restart(agent, host)
    if action == "status":
        return driver.status(agent, host)
    if action == "push_config":
        lines = list(driver.push_config(agent, host))
        return {"ok": True, "output": "\n".join(lines)}
    raise ValueError(f"unsupported action {action!r}")


def run_job(job: CronJob, now: datetime.datetime) -> dict:
    """Execute a single cron job against all its target agents and update its
    persisted last-run state. Returns a summary dict.
    """
    action = job.command.get("action")
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported cron action {action!r}")

    outputs = []
    overall_ok = True
    for agent_id in job.target_agent_ids:
        try:
            result = _run_action(job.id, agent_id, action, job.timeout)
            ok = result.get("ok", True)
            output = result.get("output", str(result))
        except Exception as exc:  # noqa: BLE001
            ok = False
            output = str(exc)
        overall_ok = overall_ok and ok
        outputs.append(f"=== {agent_id} ===\n{output}")

    # Ensure the stored timestamp is timezone-aware when possible.
    ts = now if now.tzinfo else now.replace(tzinfo=_UTC)
    updated = job.model_copy(update={
        "last_run_at": ts.isoformat(),
        "last_run_status": "ok" if overall_ok else "failed",
        "last_run_output": "\n\n".join(outputs),
    })
    cron_store.upsert_cron_job(updated)
    return {
        "job_id": job.id,
        "ran_at": updated.last_run_at,
        "status": updated.last_run_status,
        "agents": job.target_agent_ids,
    }


def tick(now: datetime.datetime | None = None, dry_run: bool = False) -> list[dict]:
    """Evaluate all enabled cron jobs and dispatch the ones due at `now`.

    In dry-run mode no job is executed; the returned records only say whether
    each enabled job would be due.
    """
    now = now or datetime.datetime.now(tz=_UTC)
    results = []
    for job in cron_store.list_cron_jobs():
        if not job.enabled:
            continue
        due = _is_due(job.schedule, now)
        if dry_run:
            results.append({"job_id": job.id, "due": due, "schedule": job.schedule, "ran_at": None})
            continue
        if due:
            results.append(run_job(job, now))
        else:
            results.append({"job_id": job.id, "due": False, "ran_at": None})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Beacon cron scheduler")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tick = sub.add_parser("tick", help="run one scheduling tick")
    p_tick.add_argument("--dry-run", action="store_true", help="report what would run without running")
    p_tick.add_argument("--utc", action="store_true", help="use UTC instead of local time")

    p_list = sub.add_parser("list", help="list cron jobs")
    p_list.add_argument("--utc", action="store_true", help="use UTC instead of local time")

    args = parser.parse_args(argv)

    if args.command == "tick":
        now = datetime.datetime.now(tz=_UTC) if args.utc else datetime.datetime.now()
        results = tick(now=now, dry_run=args.dry_run)
        print(yaml.safe_dump(results, sort_keys=False))
        return 0

    if args.command == "list":
        now = datetime.datetime.now(tz=_UTC) if args.utc else datetime.datetime.now()
        jobs = []
        for job in cron_store.list_cron_jobs():
            jobs.append({
                "id": job.id,
                "enabled": job.enabled,
                "schedule": job.schedule,
                "due": _is_due(job.schedule, now) if job.enabled else False,
                "command": job.command,
                "targets": job.target_agent_ids,
                "last_run_status": job.last_run_status,
                "last_run_at": job.last_run_at,
            })
        print(yaml.safe_dump(jobs, sort_keys=False))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
