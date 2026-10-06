"""Job creation, recipient validation and read models for jobs/certificates."""

import hashlib
import secrets
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import (
    CertificateDetails,
    CertificateOut,
    JobCreate,
    JobCreatedOut,
    JobLinks,
    JobOut,
    Progress,
    RecipientIn,
    RejectedRecipient,
)

API_PREFIX = "/api/v1"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I, easy to read aloud


class RecipientValidationError(Exception):
    def __init__(self, message: str, rejected: list[RejectedRecipient]) -> None:
        super().__init__(message)
        self.message = message
        self.rejected = rejected


class IdempotencyConflictError(Exception):
    """The Idempotency-Key was already used for a different request body."""


@dataclass
class ValidationOutcome:
    valid: list[tuple[int, RecipientIn]] = field(default_factory=list)
    rejected: list[RejectedRecipient] = field(default_factory=list)


def new_verification_code() -> str:
    return "CERT-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))


def _format_error(error: dict[str, Any]) -> str:
    location = ".".join(str(part) for part in error["loc"])
    message = error["msg"].removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


def validate_recipients(rows: Sequence[Any]) -> ValidationOutcome:
    """Validate each recipient independently and reject duplicate emails within the job."""
    outcome = ValidationOutcome()
    first_index_by_email: dict[str, int] = {}

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            outcome.rejected.append(
                RejectedRecipient(index=index, errors=["recipient must be an object"], input=row)
            )
            continue
        try:
            recipient = RecipientIn.model_validate(row)
        except ValidationError as exc:
            errors = [_format_error(e) for e in exc.errors(include_url=False)]
            outcome.rejected.append(RejectedRecipient(index=index, errors=errors, input=row))
            continue

        if recipient.email in first_index_by_email:
            first = first_index_by_email[recipient.email]
            outcome.rejected.append(
                RejectedRecipient(
                    index=index,
                    errors=[f"email: duplicate of recipient at index {first}"],
                    input=row,
                )
            )
            continue

        first_index_by_email[recipient.email] = index
        outcome.valid.append((index, recipient))

    return outcome


def request_fingerprint(payload: JobCreate) -> str:
    return hashlib.sha256(payload.model_dump_json().encode()).hexdigest()


def create_job(
    db: Session,
    payload: JobCreate,
    *,
    idempotency_key: str | None = None,
    strict: bool = False,
) -> tuple[Job, bool]:
    """Persist a job and one certificate row per recipient.

    Returns `(job, created)`; `created` is False when an earlier job with the same
    Idempotency-Key and identical payload is returned instead.
    """
    fingerprint = request_fingerprint(payload)
    if idempotency_key:
        existing = _find_by_idempotency_key(db, idempotency_key, fingerprint)
        if existing is not None:
            return existing, False

    outcome = validate_recipients(payload.recipients)
    if strict and outcome.rejected:
        raise RecipientValidationError(
            "Some recipients are invalid; nothing was created (strict mode).", outcome.rejected
        )
    if not outcome.valid:
        raise RecipientValidationError("None of the recipients are valid.", outcome.rejected)

    details = payload.certificate
    job = Job(
        title=details.title,
        course_name=details.course_name,
        issuer_name=details.issuer_name,
        issue_date=details.issue_date,
        description=details.description,
        total_recipients=len(payload.recipients),
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    job.certificates = [
        Certificate(
            row_index=index,
            recipient_name=recipient.name,
            recipient_email=recipient.email,
            grade=recipient.grade,
            status=CertificateStatus.PENDING,
            verification_code=new_verification_code(),
        )
        for index, recipient in outcome.valid
    ] + [
        Certificate(
            row_index=rejected.index,
            recipient_name=_safe_str(rejected.input, "name", 100),
            recipient_email=_safe_str(rejected.input, "email", 320),
            status=CertificateStatus.INVALID,
            error="; ".join(rejected.errors),
            validation_errors=rejected.errors,
            raw_input=rejected.input,
        )
        for rejected in outcome.rejected
    ]
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request with the same Idempotency-Key won the race.
        db.rollback()
        if idempotency_key:
            existing = _find_by_idempotency_key(db, idempotency_key, fingerprint)
            if existing is not None:
                return existing, False
        raise
    return job, True


def _find_by_idempotency_key(db: Session, key: str, fingerprint: str) -> Job | None:
    job = db.scalar(select(Job).where(Job.idempotency_key == key))
    if job is not None and job.request_fingerprint != fingerprint:
        raise IdempotencyConflictError(key)
    return job


def _safe_str(row: Any, key: str, max_length: int) -> str | None:
    if isinstance(row, dict) and isinstance(row.get(key), str):
        return row[key].strip()[:max_length] or None
    return None


# ------------------------------------------------------------------------- read models


def status_counts(db: Session, job_ids: Sequence[UUID]) -> dict[UUID, Counter[str]]:
    counts: dict[UUID, Counter[str]] = {job_id: Counter() for job_id in job_ids}
    if not job_ids:
        return counts
    rows = db.execute(
        select(Certificate.job_id, Certificate.status, func.count())
        .where(Certificate.job_id.in_(job_ids))
        .group_by(Certificate.job_id, Certificate.status)
    )
    for job_id, status, count in rows:
        counts[job_id][CertificateStatus(status).value] = count
    return counts


def build_progress(counts: Counter[str], total: int) -> Progress:
    done = sum(counts[s] for s in ("succeeded", "failed", "invalid"))
    return Progress(
        total=total,
        pending=counts["pending"],
        processing=counts["processing"],
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        invalid=counts["invalid"],
        done=done,
        percent=round(100 * done / total, 2) if total else 100.0,
    )


def job_view(job: Job, counts: Counter[str]) -> JobOut:
    base = f"{API_PREFIX}/jobs/{job.id}"
    return JobOut(
        id=job.id,
        status=job.status,
        certificate=CertificateDetails(
            title=job.title,
            course_name=job.course_name,
            issuer_name=job.issuer_name,
            issue_date=job.issue_date,
            description=job.description,
        ),
        progress=build_progress(counts, job.total_recipients),
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        links=JobLinks(job=base, certificates=f"{base}/certificates", archive=f"{base}/download"),
    )


def get_job_view(db: Session, job: Job) -> JobOut:
    return job_view(job, status_counts(db, [job.id])[job.id])


def job_created_view(db: Session, job: Job) -> JobCreatedOut:
    invalid = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.INVALID)
        .order_by(Certificate.row_index)
    )
    rejected = [
        RejectedRecipient(index=c.row_index, errors=c.validation_errors or [], input=c.raw_input)
        for c in invalid
    ]
    return JobCreatedOut(**get_job_view(db, job).model_dump(), rejected_recipients=rejected)


def certificate_view(cert: Certificate) -> CertificateOut:
    return CertificateOut(
        id=cert.id,
        job_id=cert.job_id,
        row_index=cert.row_index,
        recipient_name=cert.recipient_name,
        recipient_email=cert.recipient_email,
        grade=cert.grade,
        status=cert.status,
        error=cert.error,
        verification_code=cert.verification_code,
        attempts=cert.attempts,
        generated_at=cert.generated_at,
        download_url=(
            f"{API_PREFIX}/certificates/{cert.id}/download"
            if cert.status == CertificateStatus.SUCCEEDED
            else None
        ),
    )


def is_active(job: Job) -> bool:
    return job.status in (JobStatus.PENDING, JobStatus.PROCESSING)
