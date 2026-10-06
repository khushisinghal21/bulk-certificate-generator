import csv
import io
import zipfile
from uuid import uuid4

from tests.conftest import make_recipients
from tests.test_generation import pdf_text


def _create(client, payload, recipients=None):
    return client.post("/api/v1/jobs", json=payload(recipients)).json()


def test_list_certificates_is_paginated_and_filterable(client, payload):
    recipients = make_recipients(5) + [{"name": "Bad", "email": "nope"}]
    job = _create(client, payload, recipients)
    url = f"/api/v1/jobs/{job['id']}/certificates"

    page = client.get(url, params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 6
    assert [c["row_index"] for c in page["items"]] == [2, 3]

    succeeded = client.get(url, params={"status": "succeeded"}).json()
    assert succeeded["total"] == 5
    assert all(c["download_url"] == f"/api/v1/certificates/{c['id']}/download" for c in succeeded["items"])

    invalid = client.get(url, params={"status": "invalid"}).json()["items"]
    assert [(c["row_index"], c["download_url"]) for c in invalid] == [(5, None)]
    assert "email" in invalid[0]["error"]


def test_get_single_certificate_metadata(client, payload):
    job = _create(client, payload)
    cert = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]

    response = client.get(f"/api/v1/certificates/{cert['id']}")
    assert response.status_code == 200
    assert response.json() == cert


def test_download_single_certificate_pdf(client, payload):
    job = _create(client, payload, [{"name": "Ada Lovelace", "email": "ada@example.com"}])
    cert = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]

    response = client.get(cert["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "00001_ada-lovelace_CERT-" in response.headers["content-disposition"]
    assert "Ada Lovelace" in pdf_text(response.content)


def test_download_job_archive_contains_pdfs_and_manifest(client, payload):
    recipients = [
        {"name": "Ada Lovelace", "email": "ada@example.com"},
        {"name": "Alan Turing", "email": "alan@example.com"},
        {"name": "Bad", "email": "nope"},
    ]
    job = _create(client, payload, recipients)

    response = client.get(f"/api/v1/jobs/{job['id']}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    pdfs = sorted(n for n in archive.namelist() if n.endswith(".pdf"))
    assert len(pdfs) == 2
    assert pdfs[0].startswith("00001_ada-lovelace_") and pdfs[1].startswith("00002_alan-turing_")
    assert "Alan Turing" in pdf_text(archive.read(pdfs[1]))

    manifest = list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))
    assert [(m["name"], m["status"]) for m in manifest] == [
        ("Ada Lovelace", "succeeded"),
        ("Alan Turing", "succeeded"),
        ("Bad", "invalid"),
    ]
    assert manifest[0]["file"] == pdfs[0]
    assert manifest[2]["file"] == "" and manifest[2]["error"]


def test_archive_is_unavailable_while_job_is_running(client, payload, no_dispatch):
    job = _create(client, payload)
    response = client.get(f"/api/v1/jobs/{job['id']}/download")
    assert response.status_code == 409


def test_certificate_can_be_publicly_verified(client, payload):
    job = _create(client, payload, [{"name": "Ada Lovelace", "email": "ada@example.com"}])
    cert = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]

    response = client.get(f"/api/v1/verify/{cert['verification_code'].lower()}")

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["recipient_name"] == "Ada Lovelace"
    assert body["course_name"] == "Drone Survey Fundamentals"
    assert client.get("/api/v1/verify/CERT-DOESNOTEXIST").status_code == 404


def test_unknown_certificate_returns_404(client):
    assert client.get(f"/api/v1/certificates/{uuid4()}").status_code == 404
    assert client.get(f"/api/v1/certificates/{uuid4()}/download").status_code == 404


def test_root_redirects_to_api_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
