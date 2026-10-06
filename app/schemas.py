"""Request/response schemas (Pydantic v2)."""

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    WithJsonSchema,
    field_validator,
)

from app.config import get_settings
from app.models import CertificateStatus, JobStatus
from app.services.renderer import supports_text


def _normalize_text(value: str) -> str:
    """Collapse internal whitespace and make sure the template font can draw the text."""
    value = " ".join(value.split())
    if not supports_text(value):
        raise ValueError("contains characters that the certificate template cannot render")
    return value


# --------------------------------------------------------------------------- requests


class RecipientIn(BaseModel):
    """One certificate recipient."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    name: str = Field(min_length=1, max_length=100, examples=["Ada Lovelace"])
    email: EmailStr = Field(examples=["ada@example.com"])
    grade: str | None = Field(default=None, max_length=50, examples=["A+"])

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = _normalize_text(value)
        if not any(ch.isalpha() for ch in value):
            raise ValueError("must contain at least one letter")
        return value

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("grade", mode="before")
    @classmethod
    def _blank_grade_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("grade")
    @classmethod
    def _validate_grade(cls, value: str | None) -> str | None:
        return _normalize_text(value) if value is not None else None


class CertificateDetails(BaseModel):
    """Information shared by every certificate in the job."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default="Certificate of Completion", min_length=1, max_length=80)
    course_name: str = Field(min_length=1, max_length=150, examples=["Drone Survey Fundamentals"])
    issuer_name: str = Field(min_length=1, max_length=100, examples=["Aereo Academy"])
    issue_date: date = Field(default_factory=date.today)
    description: str | None = Field(default=None, max_length=300)

    @field_validator("title", "course_name", "issuer_name", "description")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _normalize_text(value) if value is not None else None


# Recipients are accepted as raw objects and validated one by one in the service layer, so a
# single bad row does not reject the whole request. The schema is still published in OpenAPI.
RawRecipient = Annotated[Any, WithJsonSchema(RecipientIn.model_json_schema())]


class JobCreate(BaseModel):
    certificate: CertificateDetails
    recipients: list[RawRecipient] = Field(min_length=1)

    @field_validator("recipients")
    @classmethod
    def _limit_batch_size(cls, value: list[Any]) -> list[Any]:
        limit = get_settings().max_recipients_per_job
        if len(value) > limit:
            raise ValueError(f"at most {limit} recipients are allowed per job, got {len(value)}")
        return value

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "certificate": {
                        "title": "Certificate of Completion",
                        "course_name": "Drone Survey Fundamentals",
                        "issuer_name": "Aereo Academy",
                        "issue_date": "2026-10-06",
                    },
                    "recipients": [
                        {"name": "Ada Lovelace", "email": "ada@example.com", "grade": "A+"},
                        {"name": "Alan Turing", "email": "alan@example.com"},
                    ],
                }
            ]
        }
    )


# -------------------------------------------------------------------------- responses


class RejectedRecipient(BaseModel):
    index: int = Field(description="0-based position of the recipient in the submitted list")
    errors: list[str]
    input: Any = None


class Progress(BaseModel):
    total: int
    pending: int
    processing: int
    succeeded: int
    failed: int
    invalid: int
    done: int = Field(description="succeeded + failed + invalid")
    percent: float


class JobLinks(BaseModel):
    job: str
    certificates: str
    archive: str


class JobOut(BaseModel):
    id: UUID
    status: JobStatus
    certificate: CertificateDetails
    progress: Progress
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    links: JobLinks


class JobCreatedOut(JobOut):
    rejected_recipients: list[RejectedRecipient]


class JobPage(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class CertificateOut(BaseModel):
    id: UUID
    job_id: UUID
    row_index: int
    recipient_name: str | None
    recipient_email: str | None
    grade: str | None
    status: CertificateStatus
    error: str | None
    verification_code: str | None
    attempts: int
    generated_at: datetime | None
    download_url: str | None


class CertificatePage(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int


class VerificationOut(BaseModel):
    valid: bool
    certificate_id: UUID
    verification_code: str
    recipient_name: str
    title: str
    course_name: str
    issuer_name: str
    issue_date: date
    generated_at: datetime | None
