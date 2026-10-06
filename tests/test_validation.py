import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import Certificate, CertificateStatus, Job
from app.services.jobs import validate_recipients
from tests.conftest import make_recipients


def _errors_by_index(body):
    return {r["index"]: " ".join(r["errors"]) for r in body["rejected_recipients"]}


@pytest.mark.parametrize(
    "certificate",
    [
        {"issuer_name": "Aereo"},  # missing course_name
        {"course_name": "", "issuer_name": "Aereo"},  # empty course_name
        {"course_name": "GIS", "issuer_name": "Aereo", "issue_date": "06/10/2026"},  # bad date
        {"course_name": "x" * 151, "issuer_name": "Aereo"},  # too long
    ],
)
def test_invalid_certificate_details_reject_the_request(client, certificate):
    body = {"certificate": certificate, "recipients": make_recipients(1)}
    response = client.post("/api/v1/jobs", json=body)
    assert response.status_code == 422


def test_empty_recipient_list_is_rejected(client, payload):
    assert client.post("/api/v1/jobs", json=payload([])).status_code == 422


def test_too_many_recipients_is_rejected(client, payload, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_recipients_per_job", 3)
    response = client.post("/api/v1/jobs", json=payload(make_recipients(4)))

    assert response.status_code == 422
    assert "at most 3 recipients" in response.text


def test_invalid_recipients_are_reported_and_valid_ones_still_generated(client, payload, db):
    recipients = [
        {"name": "Ada Lovelace", "email": "ada@example.com"},  # 0 valid
        {"name": "No Email"},  # 1 missing email
        {"name": "Bad Email", "email": "not-an-email"},  # 2 invalid email
        {"name": "   ", "email": "blank@example.com"},  # 3 blank name
        {"name": "Ada Again", "email": "ADA@example.com"},  # 4 duplicate (case-insensitive)
        "just a string",  # 5 not an object
        {"name": "张伟", "email": "zhang@example.com"},  # 6 unsupported by template font
        {"name": "12345", "email": "digits@example.com"},  # 7 no letters
        {"name": "Alan Turing", "email": "alan@example.com", "grade": "x" * 51},  # 8 grade too long
        {"name": "Grace Hopper", "email": "grace@example.com"},  # 9 valid
    ]
    response = client.post("/api/v1/jobs", json=payload(recipients))

    assert response.status_code == 202
    body = response.json()
    errors = _errors_by_index(body)
    assert set(errors) == {1, 2, 3, 4, 5, 6, 7, 8}
    assert "email" in errors[1] and "Field required" in errors[1]
    assert "valid email" in errors[2]
    assert "name" in errors[3]
    assert "duplicate of recipient at index 0" in errors[4]
    assert "must be an object" in errors[5]
    assert "cannot render" in errors[6]
    assert "at least one letter" in errors[7]
    assert "grade" in errors[8]
    assert body["rejected_recipients"][0]["input"] == {"name": "No Email"}

    progress = body["progress"]
    assert progress == {
        "total": 10,
        "pending": 0,
        "processing": 0,
        "succeeded": 2,
        "failed": 0,
        "invalid": 8,
        "done": 10,
        "percent": 100.0,
    }
    assert body["status"] == "completed_with_errors"
    invalid = db.scalars(select(Certificate).where(Certificate.status == CertificateStatus.INVALID)).all()
    assert all(c.file_key is None and c.error for c in invalid)


def test_strict_mode_rejects_whole_request_and_creates_nothing(client, payload, db):
    recipients = [{"name": "Ada", "email": "ada@example.com"}, {"name": "Bad", "email": "nope"}]
    response = client.post("/api/v1/jobs?strict=true", json=payload(recipients))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "strict" in detail["message"]
    assert [r["index"] for r in detail["rejected_recipients"]] == [1]
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_request_with_no_valid_recipients_is_rejected(client, payload, db):
    response = client.post("/api/v1/jobs", json=payload([{"name": "x", "email": "bad"}]))

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "None of the recipients are valid."
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_recipient_values_are_normalised():
    outcome = validate_recipients(
        [{"name": "  José   Müller ", "email": "Jose.Muller@Example.COM", "grade": "  "}]
    )
    assert outcome.rejected == []
    ((_, recipient),) = outcome.valid
    assert recipient.name == "José Müller"  # accented Latin names are supported
    assert recipient.email == "jose.muller@example.com"
    assert recipient.grade is None
