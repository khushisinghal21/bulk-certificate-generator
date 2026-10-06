# Bulk Certificate Generator

[![CI](https://github.com/khushisinghal21/bulk-certificate-generator/actions/workflows/ci.yml/badge.svg)](https://github.com/khushisinghal21/bulk-certificate-generator/actions/workflows/ci.yml)

A backend API that generates PDF certificates in bulk. A client submits **one request with many
recipients**. The API validates every recipient, generates a certificate for each valid one **in the
background**, tracks the status of every certificate, and lets the client follow progress and download
the results, one at a time or as a single ZIP.

**Tech stack:** Python 3.11+ · FastAPI · SQLAlchemy 2 · PostgreSQL (SQLite for local dev and tests) ·
Celery + Redis · ReportLab · pytest · Docker

<p align="center">
  <img src="docs/sample-certificate.png" width="700" alt="A generated certificate">
</p>

## Contents

1. [Features](#features)
2. [Quick start with Docker](#quick-start-with-docker)
3. [Local setup without Docker](#local-setup-without-docker)
4. [Running the tests](#running-the-tests)
5. [Submitting a certificate generation request](#submitting-a-certificate-generation-request)
6. [Checking progress](#checking-progress)
7. [Retrieving generated certificates](#retrieving-generated-certificates)
8. [API reference](#api-reference)
9. [Design decisions](#design-decisions)
10. [Performance](#performance)
11. [Project structure](#project-structure)
12. [Configuration](#configuration)
13. [Limitations and future work](#limitations-and-future-work)

---

## Features

- **Bulk submission:** one request with up to 10,000 recipients (configurable), as JSON or a CSV upload.
- **Validation per recipient:** invalid recipients are reported with a clear reason and their position
  in the list, while all valid recipients are still processed. An optional strict mode rejects the whole
  request instead.
- **Background generation:** the API responds `202 Accepted` in well under a second. Celery workers
  generate the PDFs.
- **Failure isolation:** if one certificate fails, it is marked `failed` with its error, and the rest of
  the job continues. Failed certificates can be retried.
- **Progress tracking:** live counts of `pending / processing / succeeded / failed / invalid` and a
  percentage.
- **Retrieval:** per-recipient results, single PDF download, and a ZIP of the whole job with a
  `manifest.csv`.
- **Extras:**
  - **Idempotency-Key:** sending the same request twice creates only one job.
  - **Public verification:** each certificate carries a unique ID and a QR code that link to a
    verification endpoint.
  - **Optional API key**, Docker Compose setup, and GitHub Actions CI on SQLite and PostgreSQL.

## Quick start with Docker

Requires Docker Desktop (or Docker Engine with Compose v2) to be running.

```bash
git clone https://github.com/khushisinghal21/bulk-certificate-generator.git
cd bulk-certificate-generator
docker compose up --build
```

This starts four containers: PostgreSQL, Redis, the API, and a Celery worker with 4 processes. Open
**http://localhost:8000**, which redirects to the interactive API documentation (Swagger UI), where every
endpoint can be tried in the browser.

To run an end-to-end demo from a second terminal (it submits 250 recipients plus 2 invalid ones, prints
progress and saves `certificates.zip`):

```bash
pip install httpx
python scripts/demo.py
```

Stop everything with `docker compose down`.

## Local setup without Docker

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

**Option A: simplest, with no Redis or worker.** Tasks run inline inside the API process:

```bash
CELERY_TASK_ALWAYS_EAGER=true uvicorn app.main:app --reload
```

This is convenient for trying the API, but each `POST` only returns after its certificates are
generated.

**Option B: real background processing.** Run each command in its own terminal:

```bash
redis-server
uvicorn app.main:app --reload
celery -A app.worker.celery_app worker --loglevel=info --concurrency=4
```

By default the app uses SQLite (`./data/certificates.db`) and stores PDFs in `./data/certificates/`.
To use PostgreSQL, set `DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/certgen`. All
settings are listed under [Configuration](#configuration). The `Makefile` has shortcuts for these
commands (`make dev`, `make run`, `make worker`, `make test`, `make up`, `make demo`).

## Running the tests

```bash
pytest                                   # 41 tests, ~3 seconds, no external services needed
ruff check . && ruff format --check .    # lint and formatting
```

The tests use a temporary SQLite database and run Celery tasks inline. Each test goes through the full
path: HTTP request → task → PDF generation → storage → database. To run the same suite against
PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/certgen_test pytest
```

CI runs lint and the full suite on both SQLite and PostgreSQL on every push.

| Requirement | Test file | What is covered |
|---|---|---|
| Creating a job | `test_create_job.py` | `202` response and links, one row per recipient, splitting into batches, idempotency, CSV upload, API key |
| Input validation | `test_validation.py` | invalid certificate details, empty or oversized lists, 9 kinds of invalid recipient, strict mode, normalisation |
| Certificate generation | `test_generation.py` | PDF contains the recipient's details, deterministic output, long and accented names, one stored file per recipient |
| Job status / progress | `test_status.py` | pending → processing → completed, completed with errors, no duplicate generation, recovery after a worker crash |
| Individual failure | `test_failures.py` | one failing certificate among several, all failing, retry after a fix, broker outage |
| Retrieving certificates | `test_retrieval.py` | pagination and filters, single PDF, ZIP with manifest, verification, `404`/`409` cases |

## Submitting a certificate generation request

`POST /api/v1/jobs` with the certificate details and the list of recipients:

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d @scripts/sample_request.json
```

Request body ([`scripts/sample_request.json`](scripts/sample_request.json)):

```json
{
  "certificate": {
    "title": "Certificate of Completion",
    "course_name": "Drone Survey Fundamentals",
    "issuer_name": "Aereo Academy",
    "issue_date": "2026-10-06",
    "description": "Awarded for completing 40 hours of hands-on training in drone survey planning and orthomosaic processing."
  },
  "recipients": [
    { "name": "Ada Lovelace", "email": "ada@example.com", "grade": "A+" },
    { "name": "Alan Turing",  "email": "alan@example.com", "grade": "A" },
    { "name": "José Müller",  "email": "jose@example.com" },
    { "name": "Grace Hopper", "email": "not-an-email" },
    { "name": "Ada Lovelace", "email": "ADA@example.com" }
  ]
}
```

**Field rules**

| Field | Required | Rules |
|---|---|---|
| `certificate.course_name` | yes | 1–150 characters |
| `certificate.issuer_name` | yes | 1–100 characters |
| `certificate.title` | no | default "Certificate of Completion", up to 80 characters |
| `certificate.issue_date` | no | ISO date (`YYYY-MM-DD`), default today |
| `certificate.description` | no | up to 300 characters |
| `recipients` | yes | 1 to 10,000 items |
| `recipients[].name` | yes | 1–100 characters, must contain a letter, extra spaces removed |
| `recipients[].email` | yes | valid email address, unique within the job (case-insensitive) |
| `recipients[].grade` | no | up to 50 characters |

**Response:** `202 Accepted`, with a `Location` header pointing to the job:

```json
{
  "id": "84d1f522-0369-447d-b408-a4b748b78943",
  "status": "pending",
  "certificate": { "title": "Certificate of Completion", "course_name": "Drone Survey Fundamentals", "...": "..." },
  "progress": { "total": 5, "pending": 3, "processing": 0, "succeeded": 0, "failed": 0, "invalid": 2, "done": 2, "percent": 40.0 },
  "created_at": "2026-10-06T17:43:09.465335Z",
  "started_at": null,
  "completed_at": null,
  "links": {
    "job": "/api/v1/jobs/84d1f522-0369-447d-b408-a4b748b78943",
    "certificates": "/api/v1/jobs/84d1f522-0369-447d-b408-a4b748b78943/certificates",
    "archive": "/api/v1/jobs/84d1f522-0369-447d-b408-a4b748b78943/download"
  },
  "rejected_recipients": [
    {
      "index": 3,
      "errors": ["email: value is not a valid email address: An email address must have an @-sign."],
      "input": { "name": "Grace Hopper", "email": "not-an-email" }
    },
    {
      "index": 4,
      "errors": ["email: duplicate of recipient at index 0"],
      "input": { "name": "Ada Lovelace", "email": "ADA@example.com" }
    }
  ]
}
```

**How different situations are handled**

| Situation | Response |
|---|---|
| Some recipients are invalid | `202`. Invalid ones are listed in `rejected_recipients`; valid ones are generated |
| Some recipients are invalid and `?strict=true` is set | `422`, nothing is created |
| No recipient is valid | `422` with the errors for every row, nothing is created |
| Missing or invalid certificate details, or too many recipients | `422` |
| Repeated request with the same `Idempotency-Key` header | `200` with the original job, no duplicate is created |
| Same `Idempotency-Key` with a different body | `409` |
| Job saved but the task queue is unreachable | `503` with the `job_id`; call `POST /api/v1/jobs/{job_id}/retry` later |

**CSV upload** (needs a header row with `name` and `email`, and optionally `grade`; other columns are
ignored):

```bash
curl -X POST http://localhost:8000/api/v1/jobs/csv \
  -F file=@scripts/sample_recipients.csv \
  -F course_name="Drone Survey Fundamentals" \
  -F issuer_name="Aereo Academy"
```

## Checking progress

```bash
curl http://localhost:8000/api/v1/jobs/{job_id}
```

| Job status | Meaning |
|---|---|
| `pending` | accepted, waiting for a worker |
| `processing` | generation has started |
| `completed` | every recipient received a certificate |
| `completed_with_errors` | finished, but some recipients were invalid or failed |
| `failed` | finished, and no certificate could be generated |

To see which recipients succeeded or failed, and why:

```bash
curl "http://localhost:8000/api/v1/jobs/{job_id}/certificates?status=failed"
```

Each item includes `row_index` (the recipient's position in the request), `status`, `error`,
`attempts`, `verification_code` and `download_url`. Results are paginated with `limit` and `offset`.

If certificates failed because of a temporary problem, generate them again with:

```bash
curl -X POST http://localhost:8000/api/v1/jobs/{job_id}/retry
```

## Retrieving generated certificates

```bash
# A single certificate (use the download_url from the list above)
curl -OJ http://localhost:8000/api/v1/certificates/{certificate_id}/download

# All certificates of a job as one ZIP (available once the job has finished)
curl -OJ http://localhost:8000/api/v1/jobs/{job_id}/download
```

The ZIP contains one PDF per certificate, named like `00001_ada-lovelace_CERT-N4MFHAXMZC.pdf` (position,
name, certificate ID). It also contains a `manifest.csv` with one line for **every** recipient, including
invalid and failed ones, so the archive makes sense on its own.

Anyone holding a certificate can check it is genuine by scanning its QR code or calling:

```bash
curl http://localhost:8000/api/v1/verify/CERT-N4MFHAXMZC
```

## API reference

All endpoints are under `/api/v1`. Full request and response schemas are at `/docs`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs` | Submit a job as JSON (`?strict=true`, optional `Idempotency-Key` header) |
| `POST` | `/jobs/csv` | Submit a job as a CSV upload |
| `GET` | `/jobs` | List jobs, newest first (`?status=`, `limit`, `offset`) |
| `GET` | `/jobs/{job_id}` | Job status and progress |
| `GET` | `/jobs/{job_id}/certificates` | Per-recipient results (`?status=`, `limit`, `offset`) |
| `POST` | `/jobs/{job_id}/retry` | Generate failed certificates again |
| `GET` | `/jobs/{job_id}/download` | ZIP of all generated certificates + `manifest.csv` |
| `GET` | `/certificates/{certificate_id}` | One certificate's details |
| `GET` | `/certificates/{certificate_id}/download` | One certificate's PDF |
| `GET` | `/verify/{code}` | Public certificate verification |
| `GET` | `/health` (no prefix) | Health check, including the database |

Authentication is off by default. If the `API_KEY` setting is set, all endpoints except
`/verify/{code}`, `/health` and the documentation pages require an `X-API-Key` header.

## Design decisions

### Architecture

```mermaid
flowchart LR
    C[Client] -->|POST /jobs| API[FastAPI]
    API -->|validate, save job + one row per recipient| DB[(PostgreSQL)]
    API -->|queue batches of certificate IDs| Q[(Redis)]
    Q --> W[Celery workers]
    W -->|claim, render PDF, save status| DB
    W -->|write PDF| S[(File storage)]
    C -->|GET status / downloads| API
    API --> S
```

### 1. Generation runs in the background, not during the request

A request can contain thousands of recipients. Generating them during the request would keep the
connection open for minutes, run into HTTP timeouts, and lose all work if the client disconnected. So
the API only validates and saves the job (which takes milliseconds), returns `202 Accepted`, and
separate worker processes do the CPU-heavy PDF rendering.

- **Why Celery + Redis instead of FastAPI's `BackgroundTasks`:** `BackgroundTasks` runs inside the web
  server process. It competes with requests for CPU, cannot be scaled separately, and loses queued work
  on a restart. Celery workers are separate processes that can be scaled independently
  (`docker compose up --scale worker=3`).
- **The database is the source of truth.** Redis only carries certificate IDs. All state lives in
  PostgreSQL, so no Celery result backend is needed, and nothing important is lost if Redis restarts.

### 2. Each recipient is a database row with its own status

Every recipient becomes a row in the `certificates` table with its own status, error message, attempt
count and file location. Job progress is calculated by counting these rows by status (using an index on
`job_id, status`) rather than storing counters on the job. Stored counters can drift out of sync when
several workers update them at once; counting the actual rows is always correct.

### 3. Validation: reject the request, or reject a single recipient

- **Problems with the request itself** (missing course name, invalid date, empty or too-long list)
  reject the whole request with `422`.
- **Problems with a single recipient** (invalid email, missing name, duplicate email) only reject that
  recipient. It is saved as `invalid` with the reasons and returned in `rejected_recipients` with its
  position. For a 5,000-person event, one typo should not block 4,999 certificates. Clients who prefer
  all-or-nothing can use `?strict=true`.
- Recipients are validated **one at a time**, so a single bad entry cannot fail the whole request.
- Names are also checked against the characters the certificate font can draw. The template uses the
  standard PDF fonts, which cover English and Western European names such as "José Müller". A name in
  another script (for example "张伟") would print as empty boxes, so it is rejected with a clear message
  instead of producing a broken certificate.

### 4. One failure never stops the rest of the job

Each certificate is generated and saved in **its own database transaction**, wrapped in a
`try/except`. If rendering fails, that certificate is marked `failed` with the error message and the
worker moves on to the next one. Progress updates in real time. Infrastructure errors, such as the
database being unavailable, are not swallowed: Celery retries the whole batch with exponential backoff.
`POST /jobs/{job_id}/retry` re-queues failed certificates once the cause is fixed.

### 5. Safe against duplicate work and worker crashes

- **Atomic claim:** before generating a certificate, a worker "claims" it with a single conditional
  update (`… SET status = 'processing' WHERE id = ? AND status = 'pending'`). If the same task is
  delivered twice, the second delivery finds nothing to claim, so no certificate is generated twice.
- **Lease timeout:** if a worker crashes while processing, its certificates stay `processing`. After
  `PROCESSING_LEASE_SECONDS` another worker may claim them again. Celery's `acks_late` setting makes
  Redis redeliver the unfinished task.
- **Deterministic, atomic files:** the same input always produces a byte-identical PDF, and files are
  written to a temporary name and then renamed. A regenerated certificate is identical, and nobody can
  download a half-written file.
- **Idempotency key:** the key is stored with a hash of the request body. Repeating a request returns
  the original job; reusing the key with a different body returns `409`.

### 6. Certificates are processed in batches

Certificate IDs are sent to workers in batches of 50 (`CHUNK_SIZE`). One task per certificate would
spend more time on queue overhead than on rendering. One task per job would put a 10,000-recipient job
on a single CPU core. Batches keep the overhead low while still letting several workers share one large
job.

### 7. The job finishes correctly even with concurrent workers

After committing its own work, each batch checks whether any certificate in the job is still `pending`
or `processing`. If none are, it sets the job's final status. The last batch to finish always sees the
work of all the others, so the job is always completed. If two batches finish at the same moment, both
calculate the same status, so the result is still correct.

### 8. PDFs are generated with ReportLab

ReportLab is pure Python, so it needs no headless browser or system libraries. Each certificate takes
about 23 ms to render and is about 5 KB. The single template is defined in code
(`app/services/renderer.py`). Long names and course titles are shrunk to fit. Each certificate has a
unique, readable ID (`CERT-XXXXXXXXXX`, avoiding look-alike characters such as 0/O and 1/I) and a QR
code that links to the verification endpoint.

### 9. Storage and downloads

The code refers to files by a storage key (`{job_id}/{certificate_id}.pdf`) through a small storage
class, so moving to S3 or Google Cloud Storage only needs a new class with the same methods. ZIP files
are built in a temporary file that stays in memory for small jobs and moves to disk for large ones, then
streamed to the client. PDFs are added without re-compressing, because PDFs are already compressed.

### 10. Database

PostgreSQL in Docker and production; SQLite for zero-setup local development and tests. CI runs the
tests on both. Statuses are stored as plain text columns, so adding a new status needs no database type
change. IDs are UUIDs, so job and certificate IDs cannot be guessed.

## Performance

Measured on a MacBook Air (Apple Silicon) with one Celery worker running 4 processes:

| Setup | Recipients | API response time | Time to generate all |
|---|---|---|---|
| Docker Compose (PostgreSQL 16, Redis 7) | 500 | 0.29 s | about 3.4 s |
| Local PostgreSQL 14 + Redis | 2,000 | 0.55 s | about 13.7 s (about 145 certificates per second) |
| Local SQLite + Redis | 1,000 | 0.28 s | about 7.4 s |

In the 2,000-recipient run, every certificate was generated exactly once (`attempts = 1`) and the job
was completed correctly by concurrent workers. Since rendering is CPU-bound, throughput grows with the
number of worker processes.

## Project structure

```
app/
├── main.py              App setup, routes, health check
├── config.py            Settings from environment variables
├── database.py          Database connection and sessions
├── models.py            Job and Certificate tables, status values
├── schemas.py           Request/response models and validation rules
├── api/
│   ├── deps.py          Shared dependencies (DB session, API key, 404 handling)
│   ├── jobs.py          Submit (JSON/CSV), list, status, results, retry, ZIP download
│   └── certificates.py  Certificate details, PDF download, verification
├── services/
│   ├── jobs.py          Recipient validation, job creation, progress calculation
│   ├── dispatch.py      Sends certificates to workers in batches; retry
│   ├── generator.py     Claim → render → store → record status; job completion
│   ├── renderer.py      The certificate template (ReportLab)
│   └── storage.py       File storage
└── worker/
    ├── celery_app.py    Celery configuration
    └── tasks.py         The batch generation task
tests/                   41 tests (see "Running the tests")
scripts/                 demo.py, sample_request.json, sample_recipients.csv
docs/                    Sample certificate (PDF and PNG)
```

## Configuration

All settings come from environment variables or a `.env` file (see [`.env.example`](.env.example)).

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/certificates.db` | Database connection URL |
| `STORAGE_DIR` | `./data/certificates` | Folder where PDFs are saved |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Redis connection for the task queue |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Run tasks inside the API process (development and tests only) |
| `CHUNK_SIZE` | `50` | Certificates per worker task |
| `PROCESSING_LEASE_SECONDS` | `300` | After how long a stuck certificate can be picked up again |
| `MAX_RECIPIENTS_PER_JOB` | `10000` | Maximum recipients per request |
| `MAX_CSV_BYTES` | `5242880` | Maximum CSV upload size (5 MB) |
| `API_KEY` | empty | If set, required in the `X-API-Key` header |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Base URL used in the QR code's verification link |

## Limitations and future work

These were left out to keep the scope focused. They would be the next steps for production use:

- **Database migrations:** tables are created automatically at startup. Production should use Alembic
  migrations.
- **Automatic recovery:** if a batch task fails permanently, its certificates stay `pending` until
  `POST /jobs/{job_id}/retry` is called. A scheduled background job (Celery beat) could re-queue them
  automatically.
- **Cloud storage:** store PDFs in S3 and give clients pre-signed download links instead of serving
  files through the API.
- **Names in any script:** add a Unicode font (such as Noto Sans and Noto Sans Devanagari) so names in
  Hindi, Chinese and other scripts can be printed.
- **Delivery:** email certificates to recipients, and call a webhook when a job finishes.
- **Multi-tenancy:** per-organisation API keys, jobs visible only to their owner, and rate limiting.
- **Monitoring:** structured logs, metrics (queue length, render time, failure rate) and a Celery
  dashboard such as Flower.
- **Very large jobs (100,000+ recipients):** accept the recipient list as an uploaded file and insert
  rows in bulk so the submit request stays fast.
