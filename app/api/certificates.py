from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import CertificateDep, DbSession
from app.api.jobs import archive_filename
from app.models import Certificate, CertificateStatus
from app.schemas import CertificateOut, VerificationOut
from app.services import jobs as job_service
from app.services.storage import get_storage

router = APIRouter(prefix="/certificates", tags=["certificates"])
# Verification is public on purpose: anyone holding a certificate can check it's genuine.
public_router = APIRouter(tags=["verification"])


@router.get("/{certificate_id}")
def get_certificate(cert: CertificateDep) -> CertificateOut:
    return job_service.certificate_view(cert)


@router.get(
    "/{certificate_id}/download",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The certificate PDF"},
        409: {"description": "Certificate has not been generated (yet)"},
    },
)
def download_certificate(cert: CertificateDep) -> FileResponse:
    if cert.status != CertificateStatus.SUCCEEDED or not cert.file_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Certificate is {cert.status.value}; no file available"
        )
    storage = get_storage()
    if not storage.exists(cert.file_key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificate file is missing from storage")
    return FileResponse(
        storage.path(cert.file_key), media_type="application/pdf", filename=archive_filename(cert)
    )


@public_router.get("/verify/{verification_code}")
def verify_certificate(verification_code: str, db: DbSession) -> VerificationOut:
    """Check a certificate ID (the one printed on the certificate / encoded in its QR code)."""
    cert = db.scalar(
        select(Certificate).where(
            Certificate.verification_code == verification_code.strip().upper(),
            Certificate.status == CertificateStatus.SUCCEEDED,
        )
    )
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No valid certificate with this ID")
    job = cert.job
    return VerificationOut(
        valid=True,
        certificate_id=cert.id,
        verification_code=cert.verification_code or "",
        recipient_name=cert.recipient_name or "",
        title=job.title,
        course_name=job.course_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        generated_at=cert.generated_at,
    )
