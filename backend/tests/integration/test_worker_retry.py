"""Worker lifecycle tests.

process_job is called directly against the real test database rather than by
running a live consumer. A real worker would make the suite wait out actual
backoff (5s + 25s per dead job) and assert on wall-clock gaps — fine for a
one-off manual check, unacceptable for a gate that runs on every commit.

process_job returns a Disposition describing what should happen to the MESSAGE,
which is exactly the seam that makes this testable: no channel, no broker, no
mock of either.
"""

import uuid

from app.core.enums import JobStatus
from app.core.security import hash_password
from app.models.job import Job
from app.models.job_attempt import JobAttempt
from app.models.user import User
from worker.main import Disposition, process_job

GOOD_CSV = {"csv_text": "name,age\nAlice,30\nBob,25\n"}


def _user(db):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"))
    db.add(user)
    db.commit()
    return user


def _job(db, job_type="csv_process", payload=None, status=JobStatus.QUEUED.value,
         attempt_count=0):
    job = Job(id=uuid.uuid4(), user_id=_user(db).id, type=job_type, status=status,
              priority=5, payload=payload if payload is not None else {},
              attempt_count=attempt_count)
    db.add(job)
    db.commit()
    return job


def _attempts(db, job):
    return (
        db.query(JobAttempt)
        .filter(JobAttempt.job_id == job.id)
        .order_by(JobAttempt.attempt_number)
        .all()
    )


def test_success_path(db):
    job = _job(db, payload=GOOD_CSV)
    disposition, tier = process_job(str(job.id))

    assert disposition is Disposition.ACK
    assert tier is None
    db.refresh(job)
    assert job.status == "SUCCESS"
    assert job.attempt_count == 1
    assert job.result["row_count"] == 2
    assert job.completed_at is not None

    rows = _attempts(db, job)
    assert [(a.attempt_number, a.status, a.error) for a in rows] == [(1, "SUCCESS", None)]


def test_permanent_failure_dies_in_one_attempt(db):
    job = _job(db, payload={})  # csv_process needs csv_text -> HandlerError
    disposition, tier = process_job(str(job.id))

    # DEAD_LETTER means the caller nacks, which routes the message to the DLQ.
    assert disposition is Disposition.DEAD_LETTER
    assert tier is None
    db.refresh(job)
    assert job.status == "DEAD"
    assert job.attempt_count == 1
    assert job.completed_at is not None

    rows = _attempts(db, job)
    assert len(rows) == 1
    assert rows[0].status == "FAILED"  # attempt-level vocabulary, not job-level
    assert "csv_text is required" in rows[0].error


def test_unknown_type_is_permanent(db):
    job = _job(db, job_type="nonexistent_type", payload={})
    disposition, _ = process_job(str(job.id))

    assert disposition is Disposition.DEAD_LETTER
    db.refresh(job)
    assert job.status == "DEAD"
    assert job.attempt_count == 1
    assert "No handler registered" in _attempts(db, job)[0].error


def test_transient_failure_schedules_a_retry(db):
    # A non-dict payload makes the handler raise AttributeError, which is not a
    # HandlerError and so counts as possibly transient.
    job = _job(db, payload="not an object")
    disposition, tier = process_job(str(job.id))

    assert disposition is Disposition.RETRY
    assert tier == 1  # first tier of the backoff ladder
    db.refresh(job)
    assert job.status == "RETRYING"
    assert job.attempt_count == 1
    assert job.completed_at is None  # only terminal states get a completion time


def test_second_failure_uses_the_second_tier(db):
    job = _job(db, payload="not an object", attempt_count=1)
    disposition, tier = process_job(str(job.id))

    assert disposition is Disposition.RETRY
    assert tier == 2
    db.refresh(job)
    assert job.status == "RETRYING"
    assert job.attempt_count == 2


def test_final_failure_exhausts_retries_and_dies(db):
    job = _job(db, payload="not an object", attempt_count=2)  # third attempt
    disposition, tier = process_job(str(job.id))

    assert disposition is Disposition.DEAD_LETTER
    assert tier is None
    db.refresh(job)
    assert job.status == "DEAD"
    assert job.attempt_count == 3


def test_full_ladder_accumulates_attempt_history(db):
    """Three attempts, three rows — the retry history the detail page renders."""
    job = _job(db, payload="not an object")
    for _ in range(3):
        process_job(str(job.id))

    db.refresh(job)
    assert job.status == "DEAD"
    rows = _attempts(db, job)
    assert [a.attempt_number for a in rows] == [1, 2, 3]
    assert all(a.status == "FAILED" for a in rows)
    assert all("AttributeError" in a.error for a in rows)


def test_already_success_job_is_skipped(db):
    job = _job(db, payload=GOOD_CSV, status="SUCCESS", attempt_count=1)
    disposition, _ = process_job(str(job.id))

    assert disposition is Disposition.ACK
    db.refresh(job)
    assert job.attempt_count == 1  # unchanged — the handler never ran again
    assert _attempts(db, job) == []


def test_already_dead_job_is_skipped(db):
    job = _job(db, payload={}, status="DEAD", attempt_count=3)
    disposition, _ = process_job(str(job.id))

    assert disposition is Disposition.ACK
    db.refresh(job)
    assert job.attempt_count == 3


def test_missing_job_is_acked_not_retried(db):
    """A message for a row that no longer exists must not loop forever."""
    disposition, _ = process_job(str(uuid.uuid4()))
    assert disposition is Disposition.ACK
