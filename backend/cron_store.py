import re
from pathlib import Path

import yaml

from .schemas import CronJob

FLEET_DIR = Path(__file__).resolve().parent.parent / "fleet"
CRON_DIR = FLEET_DIR / "cron_jobs"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class NotFound(Exception):
    pass


class InvalidId(Exception):
    pass


def _check_id(id_: str) -> None:
    if not ID_RE.match(id_):
        raise InvalidId(f"id {id_!r} must match {ID_RE.pattern}")


def _job_path(id_: str) -> Path:
    return CRON_DIR / f"{id_}.yaml"


def list_cron_jobs() -> list[CronJob]:
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for path in sorted(CRON_DIR.glob("*.yaml")):
        jobs.append(CronJob(**yaml.safe_load(path.read_text())))
    return jobs


def get_cron_job(id_: str) -> CronJob:
    _check_id(id_)
    path = _job_path(id_)
    if not path.exists():
        raise NotFound(id_)
    return CronJob(**yaml.safe_load(path.read_text()))


def upsert_cron_job(job: CronJob) -> CronJob:
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    _job_path(job.id).write_text(yaml.safe_dump(job.model_dump(), sort_keys=False))
    return job


def delete_cron_job(id_: str) -> None:
    _check_id(id_)
    path = _job_path(id_)
    if not path.exists():
        raise NotFound(id_)
    path.unlink()
