import uuid
from typing import Any

from pika.exceptions import AMQPError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.enums import JobStatus, JobType
from app.core.exceptions import ConflictError, NotFoundError, QueuePublishError
from app.models.job import Job
from app.models.job_attempt import JobAttempt
from app.models.user import User
from app.rabbitmq_client import publish_job
from app.redis_client import redis_client
from app.services.job_transitions import RequeueOutcome, requeue

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


# ------------------------------------------------------------------ admin ---


def list_dead_jobs(db: Session, page: int, page_size: int) -> tuple[list[dict], int]:
    """DEAD jobs across all users, highest priority first, then oldest.

    WHERE status + ORDER BY priority is the shape idx_jobs_status_priority was
    built for — the first query in the codebase that actually uses it.

    `id` is a tiebreaker: priority and created_at can both tie, and without a
    total order offset pagination can repeat a row or skip one across pages.
    """
    query = db.query(Job).filter(Job.status == JobStatus.DEAD.value)
    total = query.count()
    jobs = (
        query.order_by(Job.priority.desc(), Job.created_at.asc(), Job.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # The final attempt's error for each job on the page, in one query rather
    # than one per row: DISTINCT ON keeps the highest attempt_number per job.
    last_errors = {}
    if jobs:
        last_errors = dict(
            db.execute(
                select(JobAttempt.job_id, JobAttempt.error)
                .where(JobAttempt.job_id.in_([j.id for j in jobs]))
                .distinct(JobAttempt.job_id)
                .order_by(JobAttempt.job_id, JobAttempt.attempt_number.desc())
            ).all()
        )

    items = [
        {
            "id": j.id, "user_id": j.user_id, "type": j.type, "priority": j.priority,
            "attempt_count": j.attempt_count, "created_at": j.created_at,
            "completed_at": j.completed_at, "last_error": last_errors.get(j.id),
        }
        for j in jobs
    ]
    return items, total


def admin_retry_job(db: Session, job_id: uuid.UUID) -> Job:
    """Give a DEAD job a fresh retry budget and put it back in the queue.

    Unlike the reaper's requeue, this DOES change the budget — deliberately. The
    reaper rescues an attempt that was cut off through no fault of the job, so
    it must not cost anything. A DEAD job spent its whole budget legitimately;
    an admin retrying it is a new decision to give it another one.

    The budget is reset by moving attempt_base up to the attempts already spent,
    not by zeroing attempt_count: zeroing would renumber the next attempt as 1
    and turn the history into 1, 2, 3, 1, 2, 3.

    Goes through the same requeue() the reaper uses. If the publish fails the
    job is restored to exactly the DEAD state it was in — still visible in this
    view and retryable again — rather than marked FAILED, which would both hide
    it from the admin and claim that a job which ran three times never ran.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    if job.status != JobStatus.DEAD.value:
        raise ConflictError(f"Job is {job.status}, not DEAD — only dead jobs can be retried.")

    outcome = requeue(
        db,
        job_id,
        expected_status=JobStatus.DEAD.value,
        set_values={"attempt_base": job.attempt_count, "started_at": None, "completed_at": None},
        restore_values={"attempt_base": job.attempt_base, "started_at": job.started_at,
                        "completed_at": job.completed_at},
    )
    if outcome is RequeueOutcome.NOT_IN_EXPECTED_STATE:
        # Lost a race: something else moved the job between the check above and
        # the conditional UPDATE — typically a second admin retrying it too.
        raise ConflictError("Job state changed before the retry could be applied.")
    if outcome is RequeueOutcome.PUBLISH_FAILED:
        raise QueuePublishError(
            "Job could not be queued. It has been left DEAD — try the retry again."
        )

    db.refresh(job)
    return job
