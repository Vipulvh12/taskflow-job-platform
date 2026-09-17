import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.core.enums import JobStatus, JobType
from app.job_types import JOB_PAYLOAD_SCHEMAS

MAX_PAYLOAD_BYTES = 64 * 1024

# Priority is passed straight to RabbitMQ's x-max-priority queue in V2, where
# HIGHER means more urgent. Keep the range small: RabbitMQ allocates internal
# structures per priority level, so 0-10 is the documented sane band.
MIN_PRIORITY = 0
MAX_PRIORITY = 10
DEFAULT_PRIORITY = 5


class JobCreateRequest(BaseModel):
    type: JobType
    payload: dict[str, Any]
    priority: int = Field(default=DEFAULT_PRIORITY, ge=MIN_PRIORITY, le=MAX_PRIORITY)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("payload")
    @classmethod
    def payload_within_size_limit(cls, v: dict[str, Any]) -> dict[str, Any]:
        # payload lands in a JSONB column with no width limit, so without this
        # a client could push arbitrarily large documents into Postgres.
        size = len(json.dumps(v).encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"Payload must be at most {MAX_PAYLOAD_BYTES} bytes (got {size}).")
        return v

    @model_validator(mode="after")
    def payload_matches_type(self):
        """Validate the payload against the schema for this job type, so a
        malformed payload is a 422 at submission rather than a job that gets
        queued, dispatched, and only then discovered to be unrunnable.

        The raw payload dict is kept as-is (not replaced with the validated
        model's dump) so what lands in JSONB is exactly what the client sent."""
        schema = JOB_PAYLOAD_SCHEMAS.get(self.type)
        if schema is None:
            return self
        try:
            schema.model_validate(self.payload)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            location = f"payload.{field}" if field else "payload"
            raise ValueError(f"{location}: {first['msg']}") from None
        return self


class JobCreateResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus


class JobAttemptResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    attempt_number: int
    status: str
    error: str | None
    started_at: datetime
    completed_at: datetime | None


class JobResponse(BaseModel):
    """Full record for the detail view. `attempts` stays empty until the
    worker starts recording them in Phase 7/8."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    status: JobStatus
    priority: int
    # Typed Any, not dict, deliberately: JSONB accepts any JSON value, and a row
    # written outside the API (a migration, a fix-up script, an operator) can
    # hold a scalar. The request model is where "must be an object" is enforced;
    # a READ must never 500 on data that exists, least of all for a dead job
    # someone is trying to diagnose.
    payload: Any
    result: Any | None
    idempotency_key: str | None
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attempts: list[JobAttemptResponse] = []


class JobSummary(BaseModel):
    """List rows deliberately omit payload/result: a page of 100 jobs each
    carrying a 64KB payload would be a 6MB response for a table view that
    shows none of it. Fetch the detail endpoint for those."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    type: str
    status: JobStatus
    priority: int
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobListResponse(BaseModel):
    items: list[JobSummary]
    page: int
    page_size: int
    total: int
    total_pages: int
