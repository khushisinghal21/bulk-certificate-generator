import csv
import io
import logging
import re
import tempfile
import unicodedata
import zipfile
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.background import BackgroundTask

from app.api.deps import DbSession, JobDep
from app.config import get_settings
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import (
    CertificateDetails,
    CertificatePage,
    JobCreate,
    JobCreatedOut,
    JobOut,
    JobPage,
)
from app.services import dispatch
from app.services import jobs as job_service
from app.services.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        max_length=255,
        description="Optional. Re-sending a request with the same key returns the original job "
        "instead of creating a duplicate.",
    ),
]
StrictMode = Annotated[
    bool,
    Query(
        description="If true, reject the whole request when any recipient is invalid. "
        "By default invalid recipients are reported and the valid ones are still generated."
    ),
]

_CREATE_RESPONSES: dict[int | str, dict] = {
    200: {"description": "Idempotent replay: the job created earlier with this Idempotency-Key"},
    409: {"description": "Idempotency-Key reused with a different request body"},
    422: {"description": "Invalid request, or no valid recipients (or any invalid in strict mode)"},
    503: {"description": "Job stored but could not be queued; call the retry endpoint"},
}


@router.post("", status_code=status.HTTP_202_ACCEPTED, responses=_CREATE_RESPONSES)
def create_job(
    payload: JobCreate,
    response: Response,
    db: DbSession,
    idempotency_key: IdempotencyKey = None,
    strict: StrictMode = False,
) -> JobCreatedOut:
    """Submit a bulk certificate generation job (JSON).

    Returns `202 Accepted` immediately; generation runs in the background. Poll the job's
    `links.job` URL for progress.
    """
    return _submit(db, response, payload, idempotency_key, strict)


@router.post("/csv", status_code=status.HTTP_202_ACCEPTED, responses=_CREATE_RESPONSES)
def create_job_from_csv(
    response: Response,
    db: DbSession,
    file: Annotated[UploadFile, File(description="CSV with header row: name,email[,grade]")],
    course_name: Annotated[str, Form()],
    issuer_name: Annotated[str, Form()],
    issue_date: Annotated[date | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    idempotency_key: IdempotencyKey = None,
    strict: StrictMode = False,
) -> JobCreatedOut:
    """Submit a job from a CSV upload: same behaviour as the JSON endpoint."""
    max_bytes = get_settings().max_csv_bytes
    raw = file.file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"CSV larger than {max_bytes} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in reader.fieldnames or []}
    if not {"name", "email"} <= headers:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "CSV must have a header row containing at least 'name' and 'email'",
        )
    recipients = [
        {key.strip().lower(): (value or "").strip() for key, value in row.items() if key} for row in reader
    ]

    details = {
        "course_name": course_name,
        "issuer_name": issuer_name,
        "issue_date": issue_date,
        "title": title,
        "description": description,
    }
    try:
        payload = JobCreate(
            certificate=CertificateDetails(**{k: v for k, v in details.items() if v is not None}),
            recipients=recipients,
        )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False))
    return _submit(db, response, payload, idempotency_key, strict)


@router.get("")
def list_jobs(
    db: DbSession,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobPage:
    """List jobs, newest first."""
    query = select(Job)
    if status_filter is not None:
        query = query.where(Job.status == status_filter)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    jobs = list(db.scalars(query.order_by(Job.created_at.desc()).limit(limit).offset(offset)))
    counts = job_service.status_counts(db, [job.id for job in jobs])
    return JobPage(
        items=[job_service.job_view(job, counts[job.id]) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}")
def get_job(job: JobDep, db: DbSession) -> JobOut:
    """Job status and progress counts (pending / processing / succeeded / failed / invalid)."""
    return job_service.get_job_view(db, job)


@router.get("/{job_id}/certificates")
def list_job_certificates(
    job: JobDep,
    db: DbSession,
    status_filter: Annotated[CertificateStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CertificatePage:
    """Per-recipient results, in submission order. Filter with `?status=failed` etc."""
    query = select(Certificate).where(Certificate.job_id == job.id)
    if status_filter is not None:
        query = query.where(Certificate.status == status_filter)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    certs = db.scalars(query.order_by(Certificate.row_index).limit(limit).offset(offset))
    return CertificatePage(
        items=[job_service.certificate_view(c) for c in certs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "Job is still running or has nothing to retry"}},
)
def retry_job(job: JobDep, db: DbSession) -> JobOut:
    """Re-queue certificates whose generation failed (invalid recipients are not retried).

    Also re-queues certificates still pending, which covers a job whose initial enqueue failed.
    """
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job is still processing")
    reset = dispatch.reset_failed_for_retry(db, job)
    pending = db.scalar(
        select(func.count()).where(
            Certificate.job_id == job.id, Certificate.status == CertificateStatus.PENDING
        )
    )
    if not reset and not pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job has no failed certificates to retry")
    _enqueue_or_503(db, job)
    return job_service.get_job_view(db, job)


@router.get(
    "/{job_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/zip": {}}, "description": "ZIP of generated PDFs"},
        404: {"description": "Job has no generated certificates"},
        409: {"description": "Job has not finished yet"},
    },
)
def download_job_archive(job: JobDep, db: DbSession) -> StreamingResponse:
    """Download all generated certificates as a ZIP, plus a `manifest.csv` covering every
    recipient (including failed and invalid ones)."""
    if job_service.is_active(job):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Job is still {job.status.value}")
    certs = list(
        db.scalars(select(Certificate).where(Certificate.job_id == job.id).order_by(Certificate.row_index))
    )
    if not any(c.status == CertificateStatus.SUCCEEDED for c in certs):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job has no generated certificates")

    storage = get_storage()
    # Spool to memory, overflowing to disk for large jobs, so we never hold a huge ZIP in RAM.
    # The response's background task closes it once streaming finishes.
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115
    manifest = io.StringIO()
    writer = csv.writer(manifest)
    writer.writerow(
        ["row_index", "name", "email", "status", "certificate_id", "verification_code", "file", "error"]
    )
    with zipfile.ZipFile(spool, "w") as archive:
        for cert in certs:
            filename = ""
            if cert.status == CertificateStatus.SUCCEEDED and cert.file_key:
                if storage.exists(cert.file_key):
                    filename = archive_filename(cert)
                    # PDFs are already compressed; store them as-is to save CPU.
                    archive.write(storage.path(cert.file_key), filename, zipfile.ZIP_STORED)
                else:
                    logger.error("File missing for certificate %s", cert.id)
            writer.writerow(
                [
                    cert.row_index,
                    cert.recipient_name or "",
                    cert.recipient_email or "",
                    cert.status.value,
                    cert.id,
                    cert.verification_code or "",
                    filename,
                    cert.error or ("file missing" if cert.file_key and not filename else ""),
                ]
            )
        archive.writestr("manifest.csv", manifest.getvalue(), zipfile.ZIP_DEFLATED)
    spool.seek(0)

    return StreamingResponse(
        iter(lambda: spool.read(64 * 1024), b""),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates-{job.id}.zip"'},
        background=BackgroundTask(spool.close),
    )


# ---------------------------------------------------------------------------- helpers


def archive_filename(cert: Certificate) -> str:
    ascii_name = unicodedata.normalize("NFKD", cert.recipient_name or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()[:50] or "certificate"
    return f"{cert.row_index + 1:05d}_{slug}_{cert.verification_code}.pdf"


def _submit(
    db: DbSession,
    response: Response,
    payload: JobCreate,
    idempotency_key: str | None,
    strict: bool,
) -> JobCreatedOut:
    try:
        job, created = job_service.create_job(db, payload, idempotency_key=idempotency_key, strict=strict)
    except job_service.RecipientValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": exc.message,
                "rejected_recipients": [r.model_dump(mode="json") for r in exc.rejected],
            },
        )
    except job_service.IdempotencyConflictError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This Idempotency-Key was already used with a different request body",
        )

    if created:
        _enqueue_or_503(db, job)
    else:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"{job_service.API_PREFIX}/jobs/{job.id}"
    return job_service.job_created_view(db, job)


def _enqueue_or_503(db: DbSession, job: Job) -> None:
    try:
        dispatch.enqueue_job(db, job.id)
    except Exception:
        logger.exception("Could not queue job %s", job.id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Job was saved but could not be queued. "
                "Retry later with POST /api/v1/jobs/{job_id}/retry.",
                "job_id": str(job.id),
            },
        )
    # In eager mode the work already ran in other sessions; reload the job's state.
    db.refresh(job)
