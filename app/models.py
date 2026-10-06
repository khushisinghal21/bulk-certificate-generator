"""ORM models.

A *Job* is one bulk request. Every recipient in the request becomes one *Certificate* row,
which is the unit of work: it carries its own status, error and output file. Job progress
is derived from the certificate rows, so it can never drift out of sync with reality.
"""

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes on every backend (SQLite drops tzinfo on read)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"  # every recipient got a certificate
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some invalid or failed
    FAILED = "failed"  # no certificate could be generated

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_JOB_STATUSES


TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED})


class CertificateStatus(enum.StrEnum):
    PENDING = "pending"  # valid, waiting for a worker
    PROCESSING = "processing"  # claimed by a worker
    SUCCEEDED = "succeeded"  # PDF generated and stored
    FAILED = "failed"  # generation raised an error; can be retried
    INVALID = "invalid"  # rejected by validation; never generated


def _str_enum(enum_cls: type[enum.Enum]) -> Enum:
    # Stored as VARCHAR (not a native DB enum) so adding a status needs no type migration.
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[JobStatus] = mapped_column(_str_enum(JobStatus), default=JobStatus.PENDING, index=True)

    # Certificate details shared by every recipient in the job.
    title: Mapped[str] = mapped_column(String(80))
    course_name: Mapped[str] = mapped_column(String(150))
    issuer_name: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)

    total_recipients: Mapped[int] = mapped_column(Integer)

    # Lets clients retry a submission safely without creating duplicate jobs.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("job_id", "row_index", name="uq_certificates_job_row"),
        Index("ix_certificates_job_status", "job_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    # Position of the recipient in the submitted list (0-based), so clients can map
    # results and errors back to their input.
    row_index: Mapped[int] = mapped_column(Integer)

    recipient_name: Mapped[str | None] = mapped_column(String(100))
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    grade: Mapped[str | None] = mapped_column(String(50))

    status: Mapped[CertificateStatus] = mapped_column(_str_enum(CertificateStatus))
    error: Mapped[str | None] = mapped_column(Text)
    # For INVALID rows: the original input and the individual validation messages.
    raw_input: Mapped[Any | None] = mapped_column(JSON)
    validation_errors: Mapped[list[str] | None] = mapped_column(JSON)

    verification_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    file_key: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    processing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    job: Mapped[Job] = relationship(back_populates="certificates")
