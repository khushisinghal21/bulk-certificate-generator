from sqlalchemy import func, select

from app.config import get_settings
from app.models import Certificate, CertificateStatus, Job
from tests.conftest import make_recipients


def test_create_job_returns_202_with_job_details_and_links(client, payload):
    response = client.post("/api/v1/jobs", json=payload())

    assert response.status_code == 202
    body = response.json()
    job_id = body["id"]
    assert response.headers["Location"] == f"/api/v1/jobs/{job_id}"
    assert body["certificate"]["course_name"] == "Drone Survey Fundamentals"
    assert body["certificate"]["title"] == "Certificate of Completion"  # default
    assert body["progress"]["total"] == 3
    assert body["rejected_recipients"] == []
    assert body["links"] == {
        "job": f"/api/v1/jobs/{job_id}",
        "certificates": f"/api/v1/jobs/{job_id}/certificates",
        "archive": f"/api/v1/jobs/{job_id}/download",
    }


def test_create_job_persists_one_certificate_per_recipient_in_order(client, payload, db, no_dispatch):
    response = client.post("/api/v1/jobs", json=payload(make_recipients(5)))
    assert response.status_code == 202
    assert response.json()["status"] == "pending"

    certs = db.scalars(select(Certificate).order_by(Certificate.row_index)).all()
    assert [c.row_index for c in certs] == [0, 1, 2, 3, 4]
    assert [c.recipient_name for c in certs] == [f"Recipient {i}" for i in range(5)]
    assert all(c.status == CertificateStatus.PENDING for c in certs)
    codes = {c.verification_code for c in certs}
    assert len(codes) == 5 and all(code.startswith("CERT-") for code in codes)


def test_bulk_job_is_split_into_batches(client, payload, monkeypatch):
    sent = []
    from app.worker import tasks

    monkeypatch.setattr(tasks.generate_batch, "delay", lambda job_id, ids: sent.append(ids))
    client.post("/api/v1/jobs", json=payload(make_recipients(5)))

    # CHUNK_SIZE=2 in tests -> 3 batches covering all 5 certificates exactly once.
    assert [len(batch) for batch in sent] == [2, 2, 1]
    assert len({cid for batch in sent for cid in batch}) == 5


def test_same_idempotency_key_returns_original_job(client, payload, db):
    headers = {"Idempotency-Key": "event-2026-10-06"}
    first = client.post("/api/v1/jobs", json=payload(), headers=headers)
    second = client.post("/api/v1/jobs", json=payload(), headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_idempotency_key_reused_with_different_body_is_rejected(client, payload):
    headers = {"Idempotency-Key": "event-2026-10-06"}
    client.post("/api/v1/jobs", json=payload(), headers=headers)
    response = client.post("/api/v1/jobs", json=payload(course_name="Other"), headers=headers)

    assert response.status_code == 409


def test_create_job_from_csv(client, db):
    csv_body = "name,email,grade,phone\nAda Lovelace,ADA@example.com,A+,123\nAlan Turing,alan@example.com,,\n"
    response = client.post(
        "/api/v1/jobs/csv",
        files={"file": ("recipients.csv", csv_body, "text/csv")},
        data={"course_name": "GIS 101", "issuer_name": "Aereo Academy", "issue_date": "2026-10-06"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["progress"]["total"] == 2
    certs = db.scalars(select(Certificate).order_by(Certificate.row_index)).all()
    assert [(c.recipient_email, c.grade) for c in certs] == [
        ("ada@example.com", "A+"),
        ("alan@example.com", None),
    ]


def test_csv_without_required_columns_is_rejected(client):
    response = client.post(
        "/api/v1/jobs/csv",
        files={"file": ("r.csv", "full_name,mail\nAda,ada@example.com\n", "text/csv")},
        data={"course_name": "GIS 101", "issuer_name": "Aereo Academy"},
    )
    assert response.status_code == 422
    assert "name" in response.json()["detail"]


def test_api_key_is_enforced_when_configured(client, payload, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", "s3cret")

    assert client.post("/api/v1/jobs", json=payload()).status_code == 401
    ok = client.post("/api/v1/jobs", json=payload(), headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 202
    assert client.get("/health").status_code == 200  # health stays open
