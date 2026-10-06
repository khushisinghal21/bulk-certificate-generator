"""Certificate generation pipeline, executed by Celery workers.

Design notes:
* Each certificate is claimed, rendered and committed in its own short transaction, so
  progress is visible immediately and one failure never rolls back another certificate.
* Claiming is an atomic conditional UPDATE, which makes processing idempotent: if a task is
  delivered twice, the second delivery finds nothing left to claim.
* A certificate stuck in "processing" (worker crashed mid-way) can be re-claimed once its
  lease expires; Celery's `acks_late` redelivers the unfinished task.
"""

import logging
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, session_scope
from app.models import Certificate, CertificateStatus, Job, JobStatus, utcnow
from app.services import renderer
from app.services.jobs import API_PREFIX, status_counts
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 1000


def process_batch(job_id: UUID, certificate_ids: Sequence[UUID]) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("Job %s no longer exists; skipping batch", job_id)
            return
        _mark_job_started(db, job_id)
        db.expunge(job)  # read-only snapshot shared by every certificate in the batch

    for certificate_id in certificate_ids:
        generate_certificate(job, certificate_id)

    finalize_job_if_done(job_id)


def generate_certificate(job: Job, certificate_id: UUID) -> CertificateStatus | None:
    """Generate one certificate. Returns its new status, or None if it was not claimable."""
    with SessionLocal() as db:
        if not _claim(db, certificate_id):
            return None
        cert = db.get(Certificate, certificate_id)
        assert cert is not None and cert.recipient_name is not None

        try:
            pdf = renderer.render_certificate(
                renderer.CertificateContent(
                    recipient_name=cert.recipient_name,
                    course_name=job.course_name,
                    issuer_name=job.issuer_name,
                    issue_date=job.issue_date,
                    title=job.title,
                    verification_code=cert.verification_code or "",
                    verification_url=verification_url(cert.verification_code or ""),
                    grade=cert.grade,
                    description=job.description,
                )
            )
            key = get_storage().save(f"{job.id}/{cert.id}.pdf", pdf)
        except Exception as exc:  # isolate failures: one bad certificate must not stop the job
            logger.exception("Certificate %s (job %s) failed", cert.id, job.id)
            cert.status = CertificateStatus.FAILED
            cert.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
        else:
            cert.status = CertificateStatus.SUCCEEDED
            cert.file_key = key
            cert.generated_at = utcnow()
            cert.error = None
        db.commit()
        return cert.status


def finalize_job_if_done(job_id: UUID) -> JobStatus | None:
    """Set the job's final status once no certificate is pending or processing.

    Safe under concurrency: every batch calls this *after* committing its own work, so the
    last batch to finish always observes all other batches' commits. If two batches race
    here, both compute the same final status.
    """
    with session_scope() as db:
        counts = status_counts(db, [job_id])[job_id]
        if counts["pending"] or counts["processing"]:
            return None

        if counts["succeeded"] == 0:
            final = JobStatus.FAILED
        elif counts["failed"] or counts["invalid"]:
            final = JobStatus.COMPLETED_WITH_ERRORS
        else:
            final = JobStatus.COMPLETED

        db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
            .values(status=final, completed_at=utcnow())
        )
        return final


def verification_url(code: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{API_PREFIX}/verify/{code}"


def _mark_job_started(db: Session, job_id: UUID) -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.PENDING)
        .values(status=JobStatus.PROCESSING, started_at=func.coalesce(Job.started_at, utcnow()))
    )


def _claim(db: Session, certificate_id: UUID) -> bool:
    now = utcnow()
    lease_expired_before = now - timedelta(seconds=get_settings().processing_lease_seconds)
    result = db.execute(
        update(Certificate)
        .where(
            Certificate.id == certificate_id,
            or_(
                Certificate.status == CertificateStatus.PENDING,
                and_(
                    Certificate.status == CertificateStatus.PROCESSING,
                    Certificate.processing_started_at < lease_expired_before,
                ),
            ),
        )
        .values(
            status=CertificateStatus.PROCESSING,
            processing_started_at=now,
            attempts=Certificate.attempts + 1,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1
