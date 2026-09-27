from pathlib import Path

from .schemas import CronJob
from .store import FLEET_DIR, InvalidId, NotFound, check_id, dump, existing, list_dir, load  # noqa: F401 — re-exported

CRON_DIR = FLEET_DIR / "cron_jobs"


def _job_path(id_: str) -> Path:
    return CRON_DIR / f"{id_}.yaml"


def list_cron_jobs() -> list[CronJob]:
    return list_dir(CRON_DIR, CronJob)


def get_cron_job(id_: str) -> CronJob:
    check_id(id_)
    return load(_job_path(id_), id_, CronJob)


def upsert_cron_job(job: CronJob) -> CronJob:
    dump(_job_path(job.id), job)
    return job


def delete_cron_job(id_: str) -> None:
    check_id(id_)
    existing(_job_path(id_), id_).unlink()
