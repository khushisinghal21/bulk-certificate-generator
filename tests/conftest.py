"""Test configuration.

Tests use a throwaway SQLite database and storage directory, and run Celery tasks eagerly
(inline), so the full API -> task -> generator path is exercised without Redis.
Environment variables must be set before the app is imported.
"""

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from typing import Any

_TMP_DIR = tempfile.mkdtemp(prefix="certgen-tests-")
os.environ.update(
    # Set TEST_DATABASE_URL to run the suite against PostgreSQL instead (as CI does).
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP_DIR}/test.db",
    STORAGE_DIR=f"{_TMP_DIR}/files",
    CELERY_TASK_ALWAYS_EAGER="true",
    CELERY_BROKER_URL="memory://",
    API_KEY="",
    PUBLIC_BASE_URL="http://testserver",
    CHUNK_SIZE="2",  # small batches so multi-batch behaviour is exercised
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import dispatch  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    shutil.rmtree(get_settings().storage_dir, ignore_errors=True)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


@pytest.fixture
def no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept jobs without processing them, to observe pending / partial states."""

    def _enqueue_nothing(db: Session, job_id: Any) -> int:
        return 0

    monkeypatch.setattr(dispatch, "enqueue_job", _enqueue_nothing)


def make_recipients(count: int, **overrides: Any) -> list[dict[str, Any]]:
    return [
        {"name": f"Recipient {i}", "email": f"recipient{i}@example.com", **overrides} for i in range(count)
    ]


@pytest.fixture
def payload() -> Callable[..., dict[str, Any]]:
    def _payload(recipients: list[Any] | None = None, **certificate: Any) -> dict[str, Any]:
        return {
            "certificate": {
                "course_name": "Drone Survey Fundamentals",
                "issuer_name": "Aereo Academy",
                "issue_date": "2026-10-06",
                **certificate,
            },
            "recipients": recipients if recipients is not None else make_recipients(3),
        }

    return _payload
