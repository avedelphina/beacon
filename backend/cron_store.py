from . import store
from .schemas import CronJob
from .store import InvalidId, NotFound, check_id  # noqa: F401 — re-exported


def list_cron_jobs() -> list[CronJob]:
    return store.list_of("cron", CronJob)


def get_cron_job(id_: str) -> CronJob:
    check_id(id_)
    return store.load("cron", id_, CronJob)


def upsert_cron_job(job: CronJob) -> CronJob:
    check_id(job.id)
    store.save_record("cron", job.id, store.encode(job))
    return job


def delete_cron_job(id_: str) -> None:
    check_id(id_)
    store.delete_record("cron", id_)
