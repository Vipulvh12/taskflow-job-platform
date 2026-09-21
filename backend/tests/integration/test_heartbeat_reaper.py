"""Phase 15: atomic claiming, heartbeats, and the reaper.

Real Postgres and real Redis (test db 1); only the reaper's publish callback is
swapped for a recorder, since what's under test is the decision, not RabbitMQ.
"""

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.core.enums import JobStatus
from app.core.security import hash_password
from app.models.job import Job
from app.models.job_attempt import JobAttempt
from app.models.user import User
from app.redis_client import redis_client
from worker.handlers.registry import register
from worker.heartbeat import heartbeat_key, send_heartbeat
from worker.main import WORKER_ID, Disposition, process_job
from app.services.job_transitions import RequeueOutcome, requeue
from worker.reaper import REAP_GRACE_SECONDS, reap_once

GOOD_CSV = {"csv_text": "a,b\n1,2\n"}


# A probe handler that reports, from INSIDE its own execution, whether its job's
# heartbeat is live. Observing that from outside would mean racing a timer.
@register("hb_probe")
def _probe(payload, context):
    time.sleep(payload.get("sleep", 0))
    key = heartbeat_key(str(context.job_id))
    return {"heartbeat_live": bool(redis_client.exists(key)),
            "heartbeat_owner": redis_client.get(key)}


def _user(db):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com",
                password_hash=hash_password("testpass123"))
    db.add(user)
    db.commit()
    return user


def _job(db, *, job_type="csv_process", payload=None, status=JobStatus.QUEUED.value,
         attempt_count=0, started_at=None):
    job = Job(id=uuid.uuid4(), user_id=_user(db).id, type=job_type, status=status,
              priority=5, payload=GOOD_CSV if payload is None else payload,
              attempt_count=attempt_count, started_at=started_at)
    db.add(job)
    db.commit()
    return job


def _attempts(db, job):
    return db.query(JobAttempt).filter(JobAttempt.job_id == job.id).count()


def _long_ago():
    return datetime.now(timezone.utc) - timedelta(seconds=REAP_GRACE_SECONDS + 60)


class Recorder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, job_id):
        if self.fail:
            raise ConnectionError("broker down")
        self.calls.append(job_id)


# ------------------------------- claiming -------------------------------


def test_a_job_running_elsewhere_is_not_run_again(db):
    """The broker's redelivery of a job another worker holds must be skipped."""
    job = _job(db, status=JobStatus.RUNNING.value, started_at=datetime.now(timezone.utc))
    disposition, _ = process_job(str(job.id))

    assert disposition is Disposition.ACK
    db.refresh(job)
    assert job.status == "RUNNING"
    assert job.attempt_count == 0
    assert _attempts(db, job) == 0


def test_a_never_queued_job_is_not_claimable(db):
    job = _job(db, status=JobStatus.FAILED.value)
    process_job(str(job.id))
    db.refresh(job)
    assert job.status == "FAILED" and _attempts(db, job) == 0


def test_two_deliveries_of_one_job_run_it_exactly_once(db):
    """Two messages for the same job, consumed concurrently — the situation a
    crash now routinely creates (broker redelivery + reaper republish). The
    atomic claim must let exactly one of them run it."""
    job = _job(db, job_type="hb_probe", payload={"sleep": 0.5})
    results = []

    def deliver():
        results.append(process_job(str(job.id))[0])

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    db.refresh(job)
    assert job.status == "SUCCESS"
    assert job.attempt_count == 1
    assert _attempts(db, job) == 1
    assert results.count(Disposition.ACK) == 2


def test_retrying_job_is_claimable(db):
    job = _job(db, status=JobStatus.RETRYING.value, attempt_count=1)
    process_job(str(job.id))
    db.refresh(job)
    assert job.status == "SUCCESS" and job.attempt_count == 2


# ------------------------------- heartbeats -------------------------------


def test_heartbeat_is_live_while_the_handler_runs(db):
    job = _job(db, job_type="hb_probe", payload={})
    process_job(str(job.id))
    db.refresh(job)
    assert job.result["heartbeat_live"] is True
    assert job.result["heartbeat_owner"] == WORKER_ID


def test_heartbeat_survives_a_handler_longer_than_its_ttl(db, monkeypatch):
    """The background thread must keep renewing it. Shrink the TTL and interval
    so a 3s handler outlives several TTLs."""
    import worker.heartbeat as hb
    import worker.main as wm

    monkeypatch.setattr(hb, "HEARTBEAT_TTL_SECONDS", 1)
    monkeypatch.setattr(wm, "HEARTBEAT_INTERVAL_SECONDS", 0.3)
    job = _job(db, job_type="hb_probe", payload={"sleep": 3})
    process_job(str(job.id))
    db.refresh(job)
    assert job.result["heartbeat_live"] is True


def test_finished_jobs_leave_no_heartbeat(db):
    ok_job = _job(db)
    dead_job = _job(db, payload={})                          # HandlerError -> DEAD
    retry_job = _job(db, payload="not an object")            # transient -> RETRYING
    for job in (ok_job, dead_job, retry_job):
        process_job(str(job.id))
    assert redis_client.keys("job_heartbeat:*") == []


def test_a_worker_never_clears_someone_elses_heartbeat(db):
    from worker.heartbeat import clear_heartbeat

    send_heartbeat("other-worker", "some-job")
    assert clear_heartbeat(WORKER_ID, "some-job") is False
    assert redis_client.get(heartbeat_key("some-job")) == "other-worker"


# ------------------------------- the reaper -------------------------------


def test_abandoned_job_is_requeued_and_republished(db):
    job = _job(db, status=JobStatus.RUNNING.value, attempt_count=1, started_at=_long_ago())
    publish = Recorder()

    reaped = reap_once(db, publish=publish)

    assert reaped == [str(job.id)]
    assert publish.calls == [str(job.id)]
    db.refresh(job)
    assert job.status == "QUEUED"
    assert job.attempt_count == 1  # the aborted attempt costs nothing


def test_live_heartbeat_protects_a_job(db):
    job = _job(db, status=JobStatus.RUNNING.value, started_at=_long_ago())
    send_heartbeat("some-worker", str(job.id))
    publish = Recorder()

    assert reap_once(db, publish=publish) == []
    assert publish.calls == []
    db.refresh(job)
    assert job.status == "RUNNING"


def test_freshly_claimed_job_gets_a_grace_period(db):
    """Covers the gap between the worker committing RUNNING and its first beat."""
    _job(db, status=JobStatus.RUNNING.value, started_at=datetime.now(timezone.utc))
    assert reap_once(db, publish=Recorder()) == []


def test_running_row_no_worker_ever_started_is_left_alone(db):
    """No started_at means no worker ever claimed it, so there is no crashed run
    to recover. This is also what protects the seeded benchmark rows."""
    job = _job(db, status=JobStatus.RUNNING.value, started_at=None)
    assert reap_once(db, publish=Recorder()) == []
    db.refresh(job)
    assert job.status == "RUNNING"


def test_failed_publish_puts_the_job_back_for_the_next_scan(db):
    job = _job(db, status=JobStatus.RUNNING.value, started_at=_long_ago())

    assert reap_once(db, publish=Recorder(fail=True)) == []
    db.refresh(job)
    assert job.status == "RUNNING"  # not stranded at QUEUED with no message

    retry = Recorder()
    assert reap_once(db, publish=retry) == [str(job.id)]
    assert retry.calls == [str(job.id)]


def test_requeue_is_conditional_on_still_being_running(db):
    """A job that finished between the reaper's SELECT and its UPDATE must be
    left alone — and nothing published for it."""
    job = _job(db, status=JobStatus.SUCCESS.value)
    publish = Recorder()
    outcome = requeue(db, job.id, expected_status="RUNNING", publish=publish)
    assert outcome is RequeueOutcome.NOT_IN_EXPECTED_STATE
    assert publish.calls == []
    db.refresh(job)
    assert job.status == "SUCCESS"


def test_reaped_job_then_runs_exactly_once_despite_a_duplicate_message(db):
    """The full crash story. A worker died holding the job, so the job has TWO
    messages: the broker's redelivery and the reaper's republish. It must end
    SUCCESS with one attempt, whichever message arrives first."""
    job = _job(db, status=JobStatus.RUNNING.value, started_at=_long_ago())

    # Order A: the broker's redelivery arrives BEFORE the reaper has acted. The
    # job still looks RUNNING, so the worker must skip it rather than run it.
    assert process_job(str(job.id))[0] is Disposition.ACK
    db.refresh(job)
    assert job.status == "RUNNING" and _attempts(db, job) == 0

    reap_once(db, publish=Recorder())          # heartbeat long gone -> QUEUED
    process_job(str(job.id))                   # the reaper's message: runs it
    process_job(str(job.id))                   # any straggler: skipped

    db.refresh(job)
    assert job.status == "SUCCESS"
    assert job.attempt_count == 1
    assert _attempts(db, job) == 1
