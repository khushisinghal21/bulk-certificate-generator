from uuid import UUID

from sqlalchemy.exc import OperationalError

from app.services import generator
from app.worker.celery_app import celery_app


@celery_app.task(
    name="certificates.generate_batch",
    # Infrastructure hiccups (e.g. the database restarting) retry the whole batch with
    # backoff. Per-certificate rendering errors are caught inside the generator instead.
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    max_retries=5,
)
def generate_batch(job_id: str, certificate_ids: list[str]) -> None:
    generator.process_batch(UUID(job_id), [UUID(cid) for cid in certificate_ids])
