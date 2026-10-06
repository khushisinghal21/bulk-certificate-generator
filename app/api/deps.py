import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Certificate, Job

DbSession = Annotated[Session, Depends(get_db)]


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Optional shared-secret auth; enabled only when the API_KEY setting is non-empty."""
    expected = get_settings().api_key
    if expected and not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing X-API-Key header")


def get_job_or_404(job_id: UUID, db: DbSession) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Job {job_id} not found")
    return job


def get_certificate_or_404(certificate_id: UUID, db: DbSession) -> Certificate:
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Certificate {certificate_id} not found")
    return cert


JobDep = Annotated[Job, Depends(get_job_or_404)]
CertificateDep = Annotated[Certificate, Depends(get_certificate_or_404)]
