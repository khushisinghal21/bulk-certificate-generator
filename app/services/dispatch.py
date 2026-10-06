"""Hands pending certificates to the Celery workers, split into fixed-size batches."""

from collections.abc import Iterator, Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Certificate, CertificateStatus, Job, JobStatus


def _chunks(items: Sequence[UUID], size: int) -> Iterator[Sequence[UUID]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def enqueue_job(db: Session, job_id: UUID) -> int:
    """Queue every pending certificate of the job. Returns the number of tasks sent.

    Batching amortises per-task overhead (broker round-trip, DB connection, job lookup)
    while keeping tasks small enough that many workers can share one large job.
    """
    from app.worker.tasks import generate_batch  # imported lazily to avoid an import cycle

    pending_ids = list(
        db.scalars(
            select(Certificate.id)
            .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.PENDING)
            .order_by(Certificate.row_index)
        )
    )
    # End the read transaction before talking to the broker: never hold a DB transaction
    # open across network calls (and in eager mode the task needs a fresh snapshot).
    db.commit()
    tasks = 0
    for chunk in _chunks(pending_ids, get_settings().chunk_size):
        generate_batch.delay(str(job_id), [str(cid) for cid in chunk])
        tasks += 1
    return tasks


def reset_failed_for_retry(db: Session, job: Job) -> int:
    """Move FAILED certificates back to PENDING and reopen the job. Returns how many.

    The job goes back to PENDING (not PROCESSING) so that, if queueing then fails, the
    retry endpoint can simply be called again.
    """
    result = db.execute(
        update(Certificate)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.FAILED)
        .values(status=CertificateStatus.PENDING, error=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:
        job.status = JobStatus.PENDING
        job.completed_at = None
    db.commit()
    return result.rowcount
