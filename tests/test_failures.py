import pytest

from app.services import renderer

_real_render = renderer.render_certificate


@pytest.fixture
def broken_renderer(monkeypatch):
    """Make rendering fail for any recipient whose name contains 'Broken'."""

    def render(content):
        if "Broken" in content.recipient_name:
            raise RuntimeError("template engine exploded")
        return _real_render(content)

    monkeypatch.setattr(renderer, "render_certificate", render)
    return monkeypatch


def _recipients(*names):
    return [{"name": n, "email": f"{n.split()[0].lower()}{i}@example.com"} for i, n in enumerate(names)]


def test_one_failed_certificate_does_not_stop_the_others(client, payload, broken_renderer):
    recipients = _recipients("Ada Lovelace", "Broken Record", "Alan Turing", "Grace Hopper")
    job = client.post("/api/v1/jobs", json=payload(recipients)).json()

    assert job["status"] == "completed_with_errors"
    assert job["progress"]["succeeded"] == 3
    assert job["progress"]["failed"] == 1

    certs = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]
    by_name = {c["recipient_name"]: c for c in certs}
    failed = by_name["Broken Record"]
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError: template engine exploded"
    assert failed["download_url"] is None
    assert all(by_name[n]["status"] == "succeeded" for n in ("Ada Lovelace", "Alan Turing", "Grace Hopper"))

    # The failed certificate can be listed on its own and has no file to download.
    only_failed = client.get(f"/api/v1/jobs/{job['id']}/certificates?status=failed").json()
    assert [c["row_index"] for c in only_failed["items"]] == [1]
    assert client.get(f"/api/v1/certificates/{failed['id']}/download").status_code == 409


def test_job_fails_when_every_certificate_fails(client, payload, broken_renderer):
    job = client.post("/api/v1/jobs", json=payload(_recipients("Broken A", "Broken B"))).json()

    assert job["status"] == "failed"
    assert job["progress"]["failed"] == 2


def test_failed_certificates_can_be_retried(client, payload, broken_renderer):
    job = client.post("/api/v1/jobs", json=payload(_recipients("Ada Lovelace", "Broken Record"))).json()
    assert job["status"] == "completed_with_errors"

    broken_renderer.undo()  # the underlying problem has been fixed
    retried = client.post(f"/api/v1/jobs/{job['id']}/retry")

    assert retried.status_code == 202
    body = retried.json()
    assert body["status"] == "completed"
    assert body["progress"]["succeeded"] == 2
    certs = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]
    assert [c["attempts"] for c in certs] == [1, 2]


def test_retry_without_failures_is_rejected(client, payload):
    job = client.post("/api/v1/jobs", json=payload()).json()
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 409


def test_queue_outage_returns_503_and_job_can_be_retried(client, payload, monkeypatch):
    from app.worker import tasks

    def broker_down(*args, **kwargs):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(tasks.generate_batch, "delay", broker_down)
    response = client.post("/api/v1/jobs", json=payload())
    assert response.status_code == 503
    job_id = response.json()["detail"]["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "pending"

    monkeypatch.undo()  # broker is back
    retried = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retried.status_code == 202
    assert retried.json()["status"] == "completed"
