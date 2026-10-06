from datetime import date
from io import BytesIO

from pypdf import PdfReader
from sqlalchemy import select

from app.models import Certificate, CertificateStatus
from app.services.renderer import CertificateContent, render_certificate
from app.services.storage import get_storage


def _content(**overrides) -> CertificateContent:
    defaults = dict(
        recipient_name="Ada Lovelace",
        course_name="Drone Survey Fundamentals",
        issuer_name="Aereo Academy",
        issue_date=date(2026, 10, 6),
        title="Certificate of Completion",
        verification_code="CERT-ABCDEFGHJK",
        verification_url="http://testserver/api/v1/verify/CERT-ABCDEFGHJK",
        grade="A+",
        description="Forty hours of hands-on training.",
    )
    return CertificateContent(**{**defaults, **overrides})


def pdf_text(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    assert len(reader.pages) == 1
    return reader.pages[0].extract_text()


def test_rendered_pdf_contains_recipient_specific_information():
    data = render_certificate(_content())

    assert data.startswith(b"%PDF")
    text = pdf_text(data)
    for expected in (
        "Ada Lovelace",
        "Drone Survey Fundamentals",
        "AEREO ACADEMY",
        "CERTIFICATE OF COMPLETION",
        "6 October 2026",
        "with grade: A+",
        "CERT-ABCDEFGHJK",
    ):
        assert expected in text


def test_rendering_is_deterministic():
    assert render_certificate(_content()) == render_certificate(_content())


def test_long_and_accented_names_render():
    name = "Maria Fernanda de la Concepción Rodríguez-Hernández y Villaseñor Müller"
    text = pdf_text(render_certificate(_content(recipient_name=name, grade=None, description=None)))
    assert "Concepción" in text and "Villaseñor" in text


def test_job_generates_a_stored_pdf_for_every_valid_recipient(client, payload, db):
    recipients = [
        {"name": "Ada Lovelace", "email": "ada@example.com", "grade": "A+"},
        {"name": "Alan Turing", "email": "alan@example.com"},
        {"name": "Grace Hopper", "email": "grace@example.com"},
    ]
    body = client.post("/api/v1/jobs", json=payload(recipients)).json()

    assert body["status"] == "completed"
    certs = db.scalars(select(Certificate).order_by(Certificate.row_index)).all()
    storage = get_storage()
    for cert, recipient in zip(certs, recipients, strict=True):
        assert cert.status == CertificateStatus.SUCCEEDED
        assert cert.file_key == f"{body['id']}/{cert.id}.pdf"
        assert cert.generated_at is not None and cert.attempts == 1
        text = pdf_text(storage.path(cert.file_key).read_bytes())
        assert recipient["name"] in text
        assert cert.verification_code in text
