from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.models import Certificate, CertificateStatus, utcnow
from app.services import generator
from tests.conftest import make_recipients


def _certificate_ids(db, job_id: str) -> list[UUID]:
    return list(
        db.scalars(
            select(Certificate.id).where(Certificate.job_id == UUID(job_id)).order_by(Certificate.row_index)
        )
    )


def test_progress_moves_from_pending_to_processing_to_completed(client, payload, db, no_dispatch):
    job_id = client.post("/api/v1/jobs", json=payload(make_recipients(4))).json()["id"]

    pending = client.get(f"/api/v1/jobs/{job_id}").json()
    assert pending["status"] == "pending"
    assert pending["started_at"] is None
    assert pending["progress"]["pending"] == 4
    assert pending["progress"]["percent"] == 0.0

    ids = _certificate_ids(db, job_id)
    generator.process_batch(UUID(job_id), ids[:2])  # first batch only

    halfway = client.get(f"/api/v1/jobs/{job_id}").json()
    assert halfway["status"] == "processing"
    assert halfway["started_at"] is not None
    assert halfway["completed_at"] is None
    assert halfway["progress"]["succeeded"] == 2
    assert halfway["progress"]["pending"] == 2
    assert halfway["progress"]["percent"] == 50.0

    generator.process_batch(UUID(job_id), ids[2:])

    done = client.get(f"/api/v1/jobs/{job_id}").json()
    assert done["status"] == "completed"
    assert done["completed_at"] is not None
    assert done["progress"]["succeeded"] == 4
    assert done["progress"]["done"] == 4
    assert done["progress"]["percent"] == 100.0


def test_job_with_invalid_recipients_completes_with_errors(client, payload):
    recipients = make_recipients(2) + [{"name": "Bad", "email": "nope"}]
    job_id = client.post("/api/v1/jobs", json=payload(recipients)).json()["id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed_with_errors"
    assert job["progress"]["succeeded"] == 2
    assert job["progress"]["invalid"] == 1


def test_redelivered_batch_does_not_regenerate_certificates(client, payload, db):
    job_id = client.post("/api/v1/jobs", json=payload(make_recipients(2))).json()["id"]
    ids = _certificate_ids(db, job_id)

    generator.process_batch(UUID(job_id), ids)  # same batch delivered a second time

    attempts = db.scalars(select(Certificate.attempts).where(Certificate.id.in_(ids))).all()
    assert attempts == [1, 1]


def test_certificate_stuck_in_processing_is_reclaimed_after_lease_expires(client, payload, db, no_dispatch):
    job_id = client.post("/api/v1/jobs", json=payload(make_recipients(2))).json()["id"]
    stuck, fresh = _certificate_ids(db, job_id)
    # Simulate a worker that crashed long ago (stuck) and one that is still working (fresh).
    db.execute(
        update(Certificate)
        .where(Certificate.id == stuck)
        .values(status=CertificateStatus.PROCESSING, processing_started_at=utcnow() - timedelta(hours=1))
    )
    db.execute(
        update(Certificate)
        .where(Certificate.id == fresh)
        .values(status=CertificateStatus.PROCESSING, processing_started_at=utcnow())
    )
    db.commit()

    generator.process_batch(UUID(job_id), [stuck, fresh])

    db.expire_all()
    assert db.get(Certificate, stuck).status == CertificateStatus.SUCCEEDED
    assert db.get(Certificate, fresh).status == CertificateStatus.PROCESSING  # left alone
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "processing"


def test_list_jobs_includes_progress(client, payload):
    client.post("/api/v1/jobs", json=payload(make_recipients(2)))
    client.post("/api/v1/jobs", json=payload(make_recipients(1), course_name="Second"))

    page = client.get("/api/v1/jobs").json()
    assert page["total"] == 2
    assert [j["certificate"]["course_name"] for j in page["items"]] == ["Second", "Drone Survey Fundamentals"]
    assert [j["progress"]["succeeded"] for j in page["items"]] == [1, 2]
    assert client.get("/api/v1/jobs?status=failed").json()["total"] == 0


def test_unknown_job_returns_404(client):
    assert client.get(f"/api/v1/jobs/{uuid4()}").status_code == 404
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 422
