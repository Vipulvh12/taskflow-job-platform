import math
import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from app.core.enums import JobStatus, JobType
from app.core.exceptions import BadRequestError
from app.db import get_db
from app.deps import get_current_user, rate_limit_by_user
from app.models.user import User
from app.schemas.job import (
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobResponse,
)
from app.services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _resolve_idempotency_key(from_body: str | None, from_header: str | None) -> str | None:
    """The key may arrive in the body or as an Idempotency-Key header. If both
    are present and disagree, that is a client bug worth surfacing — picking
    one silently would make the request non-reproducible."""
    if from_body and from_header and from_body != from_header:
        raise BadRequestError(
            "idempotency_key in the body and the Idempotency-Key header disagree."
        )
    return from_body or from_header


@router.post("", response_model=JobCreateResponse, status_code=201)
def submit_job(
    body: JobCreateRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    # Write endpoint only — GET /jobs and GET /jobs/{id} stay on plain
    # get_current_user; reads aren't the abuse surface here.
    user: User = Depends(rate_limit_by_user("jobs:create", 100, 60)),
    db: Session = Depends(get_db),
):
    key = _resolve_idempotency_key(body.idempotency_key, idempotency_key)
    job, created = job_service.create_job(
        db, user, body.type, body.payload, body.priority, key
    )
    if not created:
        # 201 means "a job was created". A replayed key created nothing, so
        # the client can tell the two apart without diffing job ids.
        response.status_code = 200
    return JobCreateResponse(job_id=job.id, status=job.status)


@router.get("", response_model=JobListResponse)
def list_jobs(
    status: JobStatus | None = Query(default=None),
    type: JobType | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = job_service.list_jobs(
        db, user, status=status, type_=type, page=page, page_size=page_size
    )
    return JobListResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return job_service.get_job(db, user, job_id)
