import math
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_admin
from app.models.user import User
from app.schemas.admin import DeadJobListResponse, RetryResponse
from app.services import job_service

# Every route here depends on require_admin, which reads is_admin from the
# database rather than from the token's claim — so revoking admin takes effect on
# the next request, not when the access token expires. The frontend also hides
# these pages from non-admins, but that is presentation; this is the check.
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/jobs/dead", response_model=DeadJobListResponse)
def list_dead_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    items, total = job_service.list_dead_jobs(db, page, page_size)
    return DeadJobListResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.post("/jobs/{job_id}/retry", response_model=RetryResponse)
def retry_job(
    job_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    job = job_service.admin_retry_job(db, job_id)
    return RetryResponse(job_id=job.id, status=job.status, attempt_base=job.attempt_base)
