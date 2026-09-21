"""Phase 16: the admin dead-letter view and manual retry."""

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import JobStatus
from app.models.job import Job
from app.models.job_attempt import JobAttempt
from app.models.user import User
from worker.main import Disposition, process_job


class Recorder:
    def __init__(self, fail=False):
        self.calls, self.fail, self.lock = [], fail, threading.Lock()

    def __call__(self, job_id):
        if self.fail:
            raise ConnectionError("broker down")
        with self.lock:
            self.calls.append(job_id)


@pytest.fixture
def publish(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("app.services.job_transitions.publish_job", rec)
    return rec


@pytest.fixture
def admin_headers(client, db):
    email = f"admin-{uuid.uuid4().hex[:6]}@example.com"
    tokens = client.post("/auth/register", json={"email": email, "password": "testpass123"}).json()
    # Admin is never grantable through the API, by design — only in the database.
    db.query(User).filter(User.email == email).update({"is_admin": True})
    db.commit()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _owner(db):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    return user


def _dead(db, *, priority=5, attempts=3, created=None, error="AttributeError: boom",
          payload=None, attempt_base=0):
    job = Job(
        id=uuid.uuid4(), user_id=_owner(db).id, type="csv_process",
        status=JobStatus.DEAD.value, priority=priority,
        payload={"csv_text": "a,b\n1,2\n"} if payload is None else payload,
        attempt_count=attempts, attempt_base=attempt_base,
        created_at=created or datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()
    for n in range(1, attempts + 1):
        db.add(JobAttempt(job_id=job.id, attempt_number=n, status="FAILED",
                          error=f"{error} (attempt {n})",
                          started_at=datetime.now(timezone.utc),
                          completed_at=datetime.now(timezone.utc)))
    db.commit()
    return job


# ---------------------------------------------------------------- access ---


def test_non_admin_is_forbidden(client, auth_headers):
    assert client.get("/admin/jobs/dead", headers=auth_headers).status_code == 403
    resp = client.post(f"/admin/jobs/{uuid.uuid4()}/retry", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_unauthenticated_is_401(client):
    assert client.get("/admin/jobs/dead").status_code == 401


# ------------------------------------------------------------------ list ---


def test_lists_only_dead_jobs_across_all_users(client, admin_headers, db):
    dead = _dead(db)
    db.add(Job(id=uuid.uuid4(), user_id=_owner(db).id, type="csv_process",
               status="SUCCESS", priority=9, payload={}, attempt_count=1))
    db.commit()

    body = client.get("/admin/jobs/dead", headers=admin_headers).json()
    assert [i["id"] for i in body["items"]] == [str(dead.id)]
    assert body["total"] == 1


def test_ordered_by_priority_then_age_then_id(client, admin_headers, db):
    now = datetime.now(timezone.utc)
    low_old = _dead(db, priority=2, created=now - timedelta(days=5))
    high_new = _dead(db, priority=9, created=now)
    high_old = _dead(db, priority=9, created=now - timedelta(days=5))
    tie_a = _dead(db, priority=5, created=now - timedelta(days=1))
    tie_b = _dead(db, priority=5, created=now - timedelta(days=1))
    tie_b.created_at = tie_a.created_at
    db.commit()

    ids = [i["id"] for i in client.get("/admin/jobs/dead", headers=admin_headers).json()["items"]]
    ties = sorted([str(tie_a.id), str(tie_b.id)])
    assert ids == [str(high_old.id), str(high_new.id), *ties, str(low_old.id)]


def test_pagination_covers_every_row_exactly_once(client, admin_headers, db):
    """Identical priority AND created_at for every row: only the id tiebreaker
    keeps offset pagination from repeating or skipping rows."""
    same = datetime.now(timezone.utc)
    made = {str(_dead(db, priority=5, created=same).id) for _ in range(7)}

    seen = []
    for page in (1, 2, 3):
        seen += [i["id"] for i in client.get(
            f"/admin/jobs/dead?page={page}&page_size=3", headers=admin_headers).json()["items"]]
    assert len(seen) == 7 and set(seen) == made


def test_each_row_carries_its_final_attempts_error(client, admin_headers, db):
    _dead(db, attempts=3, error="ValueError: nope")
    item = client.get("/admin/jobs/dead", headers=admin_headers).json()["items"][0]
    assert item["last_error"] == "ValueError: nope (attempt 3)"


# ----------------------------------------------------------------- retry ---


def test_retry_requeues_with_a_fresh_budget(client, admin_headers, db, publish):
    job = _dead(db, attempts=3)
    resp = client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.json() == {"job_id": str(job.id), "status": "QUEUED", "attempt_base": 3}
    assert publish.calls == [str(job.id)]
    db.refresh(job)
    assert job.status == "QUEUED"
    assert job.attempt_count == 3       # history keeps counting...
    assert job.attempt_base == 3        # ...and the new budget starts here
    assert job.started_at is None and job.completed_at is None


def test_retry_of_a_non_dead_job_is_409(client, admin_headers, db, publish):
    job = _dead(db)
    job.status = "SUCCESS"
    db.commit()
    resp = client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers)
    assert resp.status_code == 409
    assert "not DEAD" in resp.json()["error"]["message"]
    assert publish.calls == []


def test_retry_of_a_missing_job_is_404(client, admin_headers, publish):
    assert client.post(f"/admin/jobs/{uuid.uuid4()}/retry",
                       headers=admin_headers).status_code == 404


def test_concurrent_retries_one_wins(client, admin_headers, db, publish):
    job = _dead(db)
    codes = []
    lock = threading.Lock()

    def retry():
        code = client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers).status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [200, 409]
    assert publish.calls == [str(job.id)]  # published exactly once


def test_failed_publish_leaves_the_job_dead_and_retryable(client, admin_headers, db,
                                                          monkeypatch):
    """Not FAILED: that would hide it from this view and claim a job that ran
    three times never ran."""
    job = _dead(db, attempts=3)
    original_completed = job.completed_at
    monkeypatch.setattr("app.services.job_transitions.publish_job", Recorder(fail=True))

    resp = client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers)
    assert resp.status_code == 502

    db.refresh(job)
    assert job.status == "DEAD"
    assert job.attempt_base == 0                    # budget change rolled back
    assert job.completed_at == original_completed
    listed = client.get("/admin/jobs/dead", headers=admin_headers).json()["items"]
    assert str(job.id) in [i["id"] for i in listed]

    ok = Recorder()
    monkeypatch.setattr("app.services.job_transitions.publish_job", ok)
    assert client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers).status_code == 200


# ------------------------------------------------ retried job, end to end ---


def test_retried_job_gets_a_full_fresh_ladder_with_continuous_numbering(
    client, admin_headers, db, publish
):
    """A transiently failing job, retried after dying: it must get three more
    attempts numbered 4, 5, 6 — not one attempt, and not a second #1."""
    job = _dead(db, attempts=3, payload="not an object")
    client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers)

    dispositions = []
    for _ in range(3):
        d, tier = process_job(str(job.id))
        dispositions.append((d, tier))
        db.refresh(job)
        if job.status == "RETRYING":
            job.status = "QUEUED"  # stand in for the delay queue expiring
            db.commit()

    assert dispositions == [(Disposition.RETRY, 1), (Disposition.RETRY, 2),
                            (Disposition.DEAD_LETTER, None)]
    db.refresh(job)
    assert job.status == "DEAD" and job.attempt_count == 6
    numbers = [a.attempt_number for a in db.query(JobAttempt)
               .filter(JobAttempt.job_id == job.id).order_by(JobAttempt.attempt_number)]
    assert numbers == [1, 2, 3, 4, 5, 6]


def test_a_retried_good_job_completes(client, admin_headers, db, publish):
    job = _dead(db, attempts=1)  # a job that died, whose payload is actually fine
    client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers)
    assert process_job(str(job.id))[0] is Disposition.ACK
    db.refresh(job)
    assert job.status == "SUCCESS" and job.attempt_count == 2


def test_budget_is_unchanged_for_jobs_never_retried(db):
    """attempt_base defaults to 0, so existing jobs keep the original ladder."""
    job = Job(id=uuid.uuid4(), user_id=_owner(db).id, type="csv_process",
              status="QUEUED", priority=5, payload="not an object", attempt_count=2)
    db.add(job)
    db.commit()
    assert process_job(str(job.id)) == (Disposition.DEAD_LETTER, None)  # 3rd of 3


def test_detail_endpoint_reports_attempt_base(client, admin_headers, db, publish):
    job = _dead(db, attempts=3)
    client.post(f"/admin/jobs/{job.id}/retry", headers=admin_headers)
    detail = client.get(f"/jobs/{job.id}", headers=admin_headers).json()
    assert detail["attempt_base"] == 3
    assert [a["attempt_number"] for a in detail["attempts"]] == [1, 2, 3]
