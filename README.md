# Bulk Certificate Generator

A backend API that takes **one request with many recipients**, validates each recipient, generates a
PDF certificate for every valid one **in the background**, tracks per-certificate status, and lets the
client poll progress and download results individually or as a single ZIP.

**Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2 · PostgreSQL (SQLite for local dev/tests) · Celery + Redis · ReportLab · pytest · Docker

<p align="center"><img src="docs/sample-certificate.png" width="720" alt="Sample generated certificate"></p>

---

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Setup and running](#setup-and-running)
- [Running tests](#running-tests)
- [API overview](#api-overview)
- [Submitting a certificate generation request](#submitting-a-certificate-generation-request)
- [Tracking progress](#tracking-progress)
- [Retrieving generated certificates](#retrieving-generated-certificates)
- [Design decisions](#design-decisions)
- [Performance](#performance)
- [Project structure](#project-structure)
- [Configuration](#configuration)
- [Limitations and next steps](#limitations-and-next-steps)

## Features

**Required**
- Bulk submission: one request, up to 10,000 recipients (configurable).
- Validation at two levels: request-level errors reject the request; **per-recipient errors are reported
  row by row while valid recipients are still generated**.
- PDF generation from a single predefined template with recipient-specific content.
- Asynchronous processing with Celery workers. The API answers `202 Accepted` in milliseconds.
- Failure isolation: a certificate that fails to generate is marked `failed` with its error, and the rest of the job carries on.
- Job status and live progress counts (`pending / processing / succeeded / failed / invalid`, percent).
- Retrieval: paginated per-recipient results, single PDF download, and a ZIP of the whole job with a `manifest.csv`.

**Extras** (each one small)
- `Idempotency-Key` header, so a client can safely resend a request without creating a duplicate job.
- `?strict=true` all-or-nothing mode.
- CSV upload endpoint (`name,email[,grade]`).
- `POST /jobs/{id}/retry` re-runs failed certificates.
- Public verification endpoint. Every certificate carries a unique ID and a QR code that links to it.
- Crash safety: an atomic per-certificate claim with a lease, plus Celery `acks_late`.
- Optional API-key authentication, Docker Compose, and CI (GitHub Actions running tests on SQLite **and** Postgres).

## Architecture

```mermaid
flowchart LR
    C[Client] -->|POST /jobs| API[FastAPI]
    API -->|1. validate + insert job and one row per recipient| DB[(PostgreSQL)]
    API -->|2. enqueue batches of N certificate ids| R[(Redis broker)]
    R --> W1[Celery worker]
    R --> W2[Celery worker]
    W1 & W2 -->|claim, render PDF, record status| DB
    W1 & W2 -->|write PDF| S[(File storage)]
    C -->|GET /jobs/id: progress| API
    C -->|GET .../download: PDF or ZIP| API
    API --> S
```

1. `POST /api/v1/jobs` validates the request and every recipient. It then stores **one job row** plus **one
   certificate row per recipient**: `pending` for valid recipients, `invalid` (with reasons) for the rest.
2. The API splits the pending certificate IDs into batches (`CHUNK_SIZE`, default 50) and sends one Celery
   task per batch. It responds `202 Accepted` with the job ID, links, and the list of rejected recipients.
3. Workers claim each certificate atomically, render the PDF, save it to storage, and commit that
   certificate's status (`succeeded` or `failed` + error) in its own short transaction.
4. When the last batch finishes, the job's final status is set: `completed`,
   `completed_with_errors` or `failed`.
5. The client polls `GET /api/v1/jobs/{id}` and then downloads PDFs or a ZIP.

## Setup and running

### Option A: Docker (closest to production)

Requires Docker with Compose v2.

```bash
docker compose up --build
```

This starts PostgreSQL, Redis, the API on <http://localhost:8000> and a Celery worker with 4 processes.
Interactive API docs (Swagger UI) are at <http://localhost:8000/docs>. To scale out, run
`docker compose up --scale worker=3`.

### Option B: Local, no Redis needed (quickest)

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
CELERY_TASK_ALWAYS_EAGER=true uvicorn app.main:app --reload
```

In eager mode, Celery tasks run **inline inside the API process**. That's convenient for trying the API,
but the `POST` request only returns once generation finishes. SQLite (`./data/certificates.db`) and local
storage (`./data/certificates/`) are used by default.

### Option C: Local with real background workers

```bash
redis-server                                                           # terminal 1 (or: docker run -p 6379:6379 redis:7)
uvicorn app.main:app --reload                                          # terminal 2
celery -A app.worker.celery_app worker --loglevel=info --concurrency=4 # terminal 3
```

To use PostgreSQL instead of SQLite, set
`DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/certgen` (see [`.env.example`](.env.example)).
The `Makefile` wraps these commands: `make install`, `make dev`, `make run`, `make worker`, `make up`, `make demo`.

To run an end-to-end demo against a running server (submits 250 recipients plus 2 invalid ones, shows
progress, saves the ZIP):

```bash
python scripts/demo.py --count 250
```

## Running tests

```bash
pytest                    # 41 tests, about 2 seconds, SQLite + eager Celery: no services needed
ruff check . && ruff format --check .
```

Run the same suite against PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/certgen_test pytest
```

The tests drive the real HTTP API through FastAPI's `TestClient`. Celery runs eagerly, so each test goes
through API → task → generator → storage → database. What they cover:

| Area (required) | File | Examples |
|---|---|---|
| Creating a job | `test_create_job.py` | 202 + links, rows persisted in order, batching into tasks, idempotency replay / conflict, CSV upload, API key |
| Input validation | `test_validation.py` | bad certificate details, empty / oversized batch, 9 kinds of invalid recipients, strict mode, no-valid-recipients, normalisation |
| Certificate generation | `test_generation.py` | PDF contains name / course / grade / date / ID, deterministic output, long and accented names, files stored per recipient |
| Job status / progress | `test_status.py` | pending → processing (50%) → completed, `completed_with_errors`, redelivery doesn't regenerate, expired lease is reclaimed |
| Individual failure | `test_failures.py` | one broken certificate among four, all failing, retry after a fix, broker outage → 503 → retry |
| Retrieval | `test_retrieval.py` | pagination and filters, metadata, single PDF, ZIP + manifest, 409 while running, public verification |

## API overview

All endpoints are under `/api/v1`. Full schemas are at `/docs` and `/openapi.json`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Submit a job (JSON). `?strict=true`, optional `Idempotency-Key` header |
| `POST` | `/jobs/csv` | Submit a job from a CSV upload (multipart) |
| `GET` | `/jobs` | List jobs (newest first, `?status=`, `limit`, `offset`) |
| `GET` | `/jobs/{job_id}` | Job status + progress counts |
| `GET` | `/jobs/{job_id}/certificates` | Per-recipient results (`?status=succeeded\|failed\|invalid\|…`, paginated) |
| `GET` | `/jobs/{job_id}/download` | ZIP of all generated PDFs + `manifest.csv` |
| `POST` | `/jobs/{job_id}/retry` | Re-queue failed (and stranded pending) certificates |
| `GET` | `/certificates/{id}` | Certificate metadata |
| `GET` | `/certificates/{id}/download` | The PDF |
| `GET` | `/verify/{code}` | **Public**: verify a certificate ID (target of the QR code) |
| `GET` | `/health` | Liveness + DB check |

If `API_KEY` is set, every endpoint except `/verify` and `/health` requires an `X-API-Key` header.

## Submitting a certificate generation request

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: course-42-batch-1" \
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
    "description": "Awarded for completing 40 hours of hands-on training ..."
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

| Field | Rules |
|---|---|
| `certificate.course_name` | required, 1–150 chars |
| `certificate.issuer_name` | required, 1–100 chars |
| `certificate.title` | optional, default "Certificate of Completion", ≤ 80 chars |
| `certificate.issue_date` | optional ISO date, default today |
| `certificate.description` | optional, ≤ 300 chars |
| `recipients` | 1 … `MAX_RECIPIENTS_PER_JOB` items |
| `recipients[].name` | required, 1–100 chars, must contain a letter; whitespace is normalised |
| `recipients[].email` | required, valid address; lower-cased; must be unique within the job |
| `recipients[].grade` | optional, ≤ 50 chars |

Response: **`202 Accepted`**, with a `Location` header pointing at the job:

```json
{
  "id": "84d1f522-0369-447d-b408-a4b748b78943",
  "status": "pending",
  "certificate": { "title": "Certificate of Completion", "course_name": "Drone Survey Fundamentals", "...": "..." },
  "progress": { "total": 5, "pending": 3, "processing": 0, "succeeded": 0, "failed": 0, "invalid": 2, "done": 2, "percent": 40.0 },
  "created_at": "2026-10-06T17:43:09.465335Z", "started_at": null, "completed_at": null,
  "links": {
    "job": "/api/v1/jobs/84d1f522-…",
    "certificates": "/api/v1/jobs/84d1f522-…/certificates",
    "archive": "/api/v1/jobs/84d1f522-…/download"
  },
  "rejected_recipients": [
    { "index": 3, "errors": ["email: value is not a valid email address: An email address must have an @-sign."],
      "input": { "name": "Grace Hopper", "email": "not-an-email" } },
    { "index": 4, "errors": ["email: duplicate of recipient at index 0"],
      "input": { "name": "Ada Lovelace", "email": "ADA@example.com" } }
  ]
}
```

| Situation | Response |
|---|---|
| Some recipients invalid (default) | `202`; invalid ones listed in `rejected_recipients`, valid ones generated |
| Some recipients invalid with `?strict=true` | `422`, nothing created |
| No valid recipients at all | `422` with every row's errors, nothing created |
| Malformed body / missing certificate fields / too many recipients | `422` |
| Same `Idempotency-Key` + same body again | `200` with the original job (no duplicate) |
| Same `Idempotency-Key` + different body | `409` |
| Job saved but broker unreachable | `503` with `job_id`; call `POST /jobs/{id}/retry` later |

**CSV upload** (header row required; extra columns are ignored):

```bash
curl -X POST http://localhost:8000/api/v1/jobs/csv \
  -F file=@scripts/sample_recipients.csv \
  -F course_name="Drone Survey Fundamentals" \
  -F issuer_name="Aereo Academy" \
  -F issue_date=2026-10-06
```

## Tracking progress

```bash
curl http://localhost:8000/api/v1/jobs/{job_id}
```

Job `status` values:

| Status | Meaning |
|---|---|
| `pending` | accepted, waiting for a worker |
| `processing` | at least one batch has started |
| `completed` | every recipient received a certificate |
| `completed_with_errors` | finished; some recipients were `invalid` or `failed` |
| `failed` | finished; no certificate could be generated |

To see exactly which recipients succeeded or failed, and why:

```bash
curl "http://localhost:8000/api/v1/jobs/{job_id}/certificates?status=failed"
```

Each item includes `row_index` (its position in your request), `status`, `error`, `attempts`,
`verification_code` and `download_url`.

## Retrieving generated certificates

```bash
# One certificate (download_url from the listing above)
curl -OJ http://localhost:8000/api/v1/certificates/{certificate_id}/download

# Everything in one ZIP, available once the job has finished (409 while it is still running)
curl -OJ http://localhost:8000/api/v1/jobs/{job_id}/download
```

The ZIP contains `00001_ada-lovelace_CERT-N4MFHAXMZC.pdf`-style files (row number + name + ID) and a
`manifest.csv` with one line per recipient, **including invalid and failed ones**, so a ZIP is
self-explanatory without calling the API.

Anyone can verify a printed certificate by scanning its QR code or calling:

```bash
curl http://localhost:8000/api/v1/verify/CERT-N4MFHAXMZC
```

## Design decisions

### 1. Background processing with Celery + Redis (not synchronous)
A single request may contain thousands of recipients. Rendering synchronously would hold the HTTP
connection open for minutes, hit proxy/gateway timeouts, and lose all work if the client disconnected.
Instead the API only **validates and persists** (milliseconds), returns `202 Accepted`, and workers do the
CPU-bound rendering.

- **Why not FastAPI `BackgroundTasks`?** They run inside the web process. They compete with request
  handling for CPU, can't scale separately, and lose queued work on restart or deploy.
- **Why Celery + Redis?** It's the standard Python answer for durable task queues, workers scale
  horizontally (`--scale worker=N`), and Redis is lightweight. The broker only carries certificate IDs.
  **All state lives in the database**, so no Celery result backend is needed and nothing important is
  lost if Redis restarts.
- For local development and tests, `CELERY_TASK_ALWAYS_EAGER=true` runs the same task code inline.

### 2. One row per recipient is the unit of work; job progress is derived
Each recipient becomes a `certificates` row with its own status, error, attempt count and file. Progress is
a `GROUP BY status` count over those rows, served by the `(job_id, status)` index, rather than counters
stored on the job. Counters can drift under concurrent updates or partial failures; a count of the actual
rows can't. The job row stores only lifecycle timestamps and the final status.

### 3. Validation: reject the request vs reject a row
- **Request-level** problems (missing course name, bad date, empty or oversized list) → `422`, nothing created.
- **Recipient-level** problems → that row is stored as `invalid` with readable reasons and echoed back in
  `rejected_recipients` with its `index`. The other rows are still generated. For a 5,000-person event,
  one typo shouldn't block 4,999 certificates. Clients that prefer all-or-nothing use `?strict=true`.
- Recipients are validated **one at a time** in the service layer (not as one Pydantic list), so one bad
  row can't fail the whole request. The OpenAPI schema still documents the recipient shape.
- Checks include duplicate emails within a job (case-insensitive) and **names the template font can't
  draw**. The template uses PDF standard fonts (Windows-1252: English and Western European names like
  "José Müller"). A name like "张伟" would otherwise render as empty boxes, so it's rejected up front
  with a clear message instead of producing a broken certificate. See next steps for Unicode fonts.

### 4. Failure isolation
Each certificate is claimed, rendered, saved and committed in **its own transaction**, inside a
`try/except` that records `failed` + `ExceptionType: message`. One failure never rolls back or blocks
another certificate, and progress is visible in real time. Infrastructure errors (e.g. the database being
down) are *not* swallowed: they propagate, and Celery retries the batch with exponential backoff.
`POST /jobs/{id}/retry` re-queues failed certificates once the cause is fixed.

### 5. Safe under retries, redelivery and crashes
- **Atomic claim:** a worker takes a certificate with a conditional `UPDATE … SET status='processing'
  WHERE id=? AND status='pending'`. If a task is delivered twice, the second delivery claims nothing,
  so there's no double work (tested).
- **Lease:** a certificate stuck in `processing` longer than `PROCESSING_LEASE_SECONDS` (its worker
  crashed) can be claimed again (tested). Celery's `acks_late` + `reject_on_worker_lost` make the broker
  redeliver the unfinished batch.
- **Deterministic PDFs** (ReportLab `invariant=True`) and **atomic file writes** (temp file + rename)
  mean a regenerated certificate is byte-identical, and readers never see a half-written file.
- **Idempotency-Key:** stored with a hash of the request body; a replay returns the original job, and a
  different body under the same key gets `409`. A unique constraint handles two concurrent submissions.

### 6. Batching
Pending IDs are sent in batches of `CHUNK_SIZE` (default 50). One task per certificate would spend more
time on broker round-trips than on rendering (a certificate takes a few milliseconds). One task per job
would put a 10,000-recipient job on a single core. Batches of 50 keep overhead low while letting many
workers share one large job. `worker_prefetch_multiplier=1` stops one worker from hoarding batches.

### 7. Race-free job completion
Every batch calls `finalize_job_if_done` *after* committing its own work. It sets the final status only
if no certificate is `pending` or `processing`. The last batch to commit always sees everyone else's
commits, so the job is always finalized. If two batches finish at the same moment, both compute the same
status, and the conditional `UPDATE … WHERE status IN ('pending','processing')` makes that harmless.

### 8. PDF generation with ReportLab
ReportLab is pure Python: no headless browser, no system libraries like wkhtmltopdf or Cairo. It's
fast enough (≈23 ms per certificate on one core, including the QR code) and produces small (~5 KB)
vector PDFs that print well. The template is a
single code-defined design (`app/services/renderer.py`). Long names and course titles shrink to fit, then
wrap. Each certificate has a unique human-friendly ID (`CERT-XXXXXXXXXX`, no ambiguous 0/O/1/I) and a
QR code to the public verification endpoint.

### 9. Storage and downloads
Code works with **storage keys** (`{job_id}/{certificate_id}.pdf`), never absolute paths, behind a small
`LocalFileStorage` class, so moving to S3/GCS means adding one class with the same interface. The ZIP is
built in a spooled temp file (memory first, disk for big jobs) and streamed. PDFs are added with
`ZIP_STORED` because they're already compressed, so deflating them again would only waste CPU.

### 10. Database
PostgreSQL in Docker/production, SQLite for zero-setup development and tests. CI runs the suite on
both. Status enums are stored as `VARCHAR` with application-level validation, so adding a status
doesn't need a database-type migration. UUID primary keys mean job IDs can't be enumerated.

## Performance

Measured on a MacBook Air (Apple Silicon), with one Celery worker running 4 processes:

| Setup | Recipients | `POST` latency | Total generation time |
|---|---|---|---|
| PostgreSQL 14 + Redis | 2,000 | 0.55 s | ≈ 13.7 s (≈ 145 certificates/s) |
| SQLite + Redis | 1,000 | 0.28 s | ≈ 7.4 s |

Every certificate was attempted exactly once (`attempts = 1`) and the job finalized correctly with
concurrent workers. Throughput scales with worker processes, since rendering is CPU-bound.

## Project structure

```
app/
  main.py              FastAPI app factory, router wiring, /health
  config.py            Settings from environment variables (.env supported)
  database.py          Engine, session factory, init_db
  models.py            Job and Certificate ORM models + status enums
  schemas.py           Pydantic request/response models and validation rules
  api/
    deps.py            DB session, API-key auth, 404 helpers
    jobs.py            Submit (JSON/CSV), list, status, per-recipient results, retry, ZIP
    certificates.py    Certificate metadata, PDF download, public verification
  services/
    jobs.py            Recipient validation, job creation, idempotency, progress read models
    dispatch.py        Splits pending certificates into Celery batches; retry reset
    generator.py       Claim → render → store → record status; job finalisation
    renderer.py        The certificate template (ReportLab)
    storage.py         Local file storage behind a key-based interface
  worker/
    celery_app.py      Celery configuration
    tasks.py           generate_batch task
tests/                 41 tests (see table above)
scripts/               demo.py, sample_request.json, sample_recipients.csv
docs/                  Sample certificate (PDF + PNG)
```

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/certificates.db` | SQLAlchemy URL (`postgresql+psycopg://…` for Postgres) |
| `STORAGE_DIR` | `./data/certificates` | Where PDFs are written |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Celery broker |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Run tasks inline (dev/tests only) |
| `CHUNK_SIZE` | `50` | Certificates per Celery task |
| `PROCESSING_LEASE_SECONDS` | `300` | When a stuck `processing` certificate can be reclaimed |
| `MAX_RECIPIENTS_PER_JOB` | `10000` | Upper bound per request |
| `MAX_CSV_BYTES` | `5242880` | Upload limit for the CSV endpoint |
| `API_KEY` | *(empty)* | If set, required in the `X-API-Key` header |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Base of the verification URL in QR codes |

## Limitations and next steps

These are deliberately out of scope for the assignment. This is what I'd do next for production:

- **Migrations:** tables are created with `create_all` at startup. Production should use **Alembic**.
- **Stranded jobs:** if a batch task fails permanently (after its retries), its certificates stay
  `pending` until someone calls `POST /jobs/{id}/retry`. A periodic **Celery beat sweeper** should
  re-queue those and expired leases automatically.
- **Object storage:** add an `S3Storage` class and serve downloads through **pre-signed URLs**
  instead of streaming through the API.
- **Unicode names:** register a Unicode TTF (e.g. Noto Sans + Noto Sans Devanagari) in the renderer
  and relax the font check, so names in any script render correctly.
- **Delivery:** optional emailing of certificates and a **webhook** callback when a job finishes.
- **Multi-tenancy and auth:** per-organisation API keys or OAuth, jobs scoped to their owner, rate limiting.
- **Observability:** structured JSON logs with job IDs, Prometheus metrics (queue depth, render time,
  failure rate), Flower for Celery.
- **Very large jobs:** accept the recipient list as an uploaded file and insert rows in batches
  (or with Postgres `COPY`) to keep the submit request fast at 100k+ recipients.
