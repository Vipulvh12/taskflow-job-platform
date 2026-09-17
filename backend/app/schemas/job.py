import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import JobStatus, JobType

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
    payload: dict[str, Any]
    result: dict[str, Any] | None
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
