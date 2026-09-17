import uuid
from typing import Any

from pika.exceptions import AMQPError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.enums import JobStatus, JobType
from app.core.exceptions import ConflictError, NotFoundError, QueuePublishError
from app.models.job import Job
from app.models.user import User
from app.rabbitmq_client import publish_job
from app.redis_client import redis_client

IDEMPOTENCY_KEY_PREFIX = "idempotency:"
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def _idempotency_cache_key(user_id: uuid.UUID, key: str) -> str:
    # Scoped per user, matching the (user_id, idempotency_key) unique
    # constraint — one user's key must never collide with another's.
    return f"{IDEMPOTENCY_KEY_PREFIX}{user_id}:{key}"


def _cache_idempotency(user_id: uuid.UUID, key: str, job_id: uuid.UUID) -> None:
    redis_client.setex(_idempotency_cache_key(user_id, key), IDEMPOTENCY_TTL_SECONDS, str(job_id))


def _assert_same_request(
    job: Job, type_: JobType, payload: dict[str, Any], priority: int
) -> None:
    """A replayed idempotency key must describe the SAME request. Reusing a
    key with a different body is a client bug; returning the old job's id
    silently would mean the new job never runs and nobody finds out."""
    if job.type != type_.value or job.priority != priority or job.payload != payload:
        raise ConflictError(
            "This idempotency key was already used for a different request. "
            "Use a new key, or resend the original request unchanged."
        )


def _publish_or_mark_failed(db: Session, job: Job) -> None:
    """Called exactly once, right after a BRAND-NEW job row is committed —
    never on the idempotency-replay path (Phase 5's 200 response), since
    that job was already published, or already failed, the first time it
    was created.

    The row and the message live in two systems with no shared transaction.
    If the commit lands but the publish doesn't, the alternative to this is
    a job stuck at QUEUED forever with nothing to process it — a silent
    failure. Marking it FAILED and returning 502 is worse for the client
    but visible, which is the trade worth making. A transactional outbox
    is the real fix (V2/V3)."""
    try:
        publish_job(str(job.id))
    except AMQPError:
        job.status = JobStatus.FAILED.value
        db.commit()
        raise QueuePublishError(
            "Job was created but could not be queued for processing. "
            "It has been marked FAILED — please retry with a new request."
        )


def create_job(
    db: Session,
    user: User,
    type_: JobType,
    payload: dict[str, Any],
    priority: int,
    idempotency_key: str | None,
) -> tuple[Job, bool]:
    """Returns (job, created). `created` is False when an idempotency key
    replayed an existing job, which the router turns into a 200 instead of
    a 201."""
    if idempotency_key:
        cached_job_id = redis_client.get(_idempotency_cache_key(user.id, idempotency_key))
        if cached_job_id:
            existing = db.get(Job, uuid.UUID(cached_job_id))
            # A cache entry pointing at a row that is gone (or at another
            # user's row) is stale, not authoritative — fall through and let
            # the DB constraint decide.
            if existing is not None and existing.user_id == user.id:
                _assert_same_request(existing, type_, payload, priority)
                return existing, False

    job = Job(
        user_id=user.id,
        type=type_.value,
        status=JobStatus.QUEUED.value,
        priority=priority,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not idempotency_key:
            # Nothing else on this table can collide, so an IntegrityError
            # without a key is a real bug — don't swallow it.
            raise
        # Two requests with the same key raced and the other one won. THIS is
        # the actual idempotency guarantee; the Redis check above is only a
        # shortcut that skips reaching this path.
        existing = (
            db.query(Job)
            .filter(Job.user_id == user.id, Job.idempotency_key == idempotency_key)
            .first()
        )
        if existing is None:
            raise
        _assert_same_request(existing, type_, payload, priority)
        _cache_idempotency(user.id, idempotency_key, existing.id)
        return existing, False

    db.refresh(job)
    # Publish before caching the idempotency key: if the publish fails this
    # raises, and there's no point caching a pointer to a job that was never
    # queued. The DB row (and its unique constraint) still holds the key.
    _publish_or_mark_failed(db, job)
    if idempotency_key:
        _cache_idempotency(user.id, idempotency_key, job.id)
    return job, True


def get_job(db: Session, user: User, job_id: uuid.UUID) -> Job:
    job = (
        db.query(Job)
        .options(selectinload(Job.attempts))
        .filter(Job.id == job_id)
        .first()
    )
    # Someone else's job is reported as missing rather than forbidden: a 403
    # would confirm the id exists.
    if job is None or (job.user_id != user.id and not user.is_admin):
        raise NotFoundError(f"Job {job_id} not found.")
    return job


def list_jobs(
    db: Session,
    user: User,
    status: JobStatus | None = None,
    type_: JobType | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Job], int]:
    query = db.query(Job).filter(Job.user_id == user.id)
    if status is not None:
        query = query.filter(Job.status == status.value)
    if type_ is not None:
        query = query.filter(Job.type == type_.value)

    total = query.count()
    items = (
        # id is a tiebreaker so rows with identical created_at can't swap
        # places between pages and cause a job to be shown twice or skipped.
        query.order_by(Job.created_at.desc(), Job.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total
