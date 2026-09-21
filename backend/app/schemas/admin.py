import uuid
from datetime import datetime

from pydantic import BaseModel


class DeadJobSummary(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    priority: int
    attempt_count: int
    created_at: datetime
    completed_at: datetime | None
    # The error from the job's final attempt. The point of this view is deciding
    # whether a retry is worth it, and that turns on WHY it died: a malformed
    # payload will die again identically, a transient failure may not.
    last_error: str | None = None


class DeadJobListResponse(BaseModel):
    items: list[DeadJobSummary]
    page: int
    page_size: int
    total: int
    total_pages: int


class RetryResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    # Attempts spent before this retry. The fresh budget is counted from here,
    # so the next attempt is numbered attempt_base + 1, not 1.
    attempt_base: int
