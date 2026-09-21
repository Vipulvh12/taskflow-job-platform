"""Detects jobs whose worker died mid-run and puts them back in the queue.

A crashed worker cannot report its own crash, so this runs as a separate
process. It is the ONLY thing allowed to move a job out of RUNNING other than
the worker that claimed it — the counterpart to the worker's atomic claim.
Between them, every state transition has exactly one owner:

    QUEUED/RETRYING -> RUNNING    the worker, via a conditional UPDATE
    RUNNING -> terminal/RETRYING  that same worker, when the attempt ends
    RUNNING -> QUEUED             the reaper, via a conditional UPDATE,
                                  only once the job's heartbeat has expired

Scaling to several reapers is safe for the same reason scaling workers is: the
conditional UPDATE lets exactly one of them win a given job.

Known limit: heartbeats are sent from a background thread, so they prove the
worker PROCESS is alive, not that the job is making progress. A handler stuck
forever keeps its heartbeat alive and is never reaped. Catching that needs a
per-job execution timeout, which is separate work.

    python -m worker.reaper
"""

import logging
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.enums import JobStatus
from app.db import SessionLocal
from app.models.job import Job
from app.rabbitmq_client import publish_job
from app.redis_client import redis_client
from worker.heartbeat import HEARTBEAT_TTL_SECONDS, live_heartbeats

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow.reaper")

SCAN_INTERVAL_SECONDS = 10

# A job is only eligible once it has been RUNNING for at least a full heartbeat
# TTL. The worker commits RUNNING and then sends its first beat, so without this
# a scan landing between those two statements would see a live job with no
# heartbeat and requeue it.
REAP_GRACE_SECONDS = HEARTBEAT_TTL_SECONDS


def _set_status(db, jid: uuid.UUID, expected: str, new: str) -> bool:
    row = db.execute(
        update(Job)
        .where(Job.id == jid, Job.status == expected)
        .values(status=new)
        .returning(Job.id)
    ).first()
    db.commit()
    return row is not None


def reap_once(db, publish=publish_job, now: datetime | None = None) -> list[str]:
    """One scan. Returns the ids of the jobs it requeued."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=REAP_GRACE_SECONDS)

    # `started_at IS NOT NULL` is the definition of abandoned, not a filter of
    # convenience: a job is only abandoned if a worker actually started it and
    # then went silent. The worker's claim always sets started_at, so a RUNNING
    # row without one was never picked up by any worker — it was written some
    # other way (a manual edit, a data load), and there is no crashed run to
    # recover. It happens to also exclude Phase 14's seeded benchmark rows,
    # ~33,000 of which are RUNNING; without this rule the first scan would
    # requeue every one of them into a handler that rejects their payload.
    candidates = [
        str(jid)
        for jid in db.execute(
            select(Job.id).where(
                Job.status == JobStatus.RUNNING.value,
                Job.started_at.is_not(None),
                Job.started_at < cutoff,
            )
        ).scalars()
    ]
    if not candidates:
        return []

    alive = live_heartbeats(candidates)
    reaped = []
    for job_id in candidates:
        if alive[job_id]:
            continue
        jid = uuid.UUID(job_id)

        # Conditional: if the job finished between the SELECT above and now,
        # this matches nothing and we leave it alone.
        if not _set_status(db, jid, JobStatus.RUNNING.value, JobStatus.QUEUED.value):
            continue

        try:
            publish(job_id)
        except Exception:  # noqa: BLE001
            # Phase 6's dual-write problem again. A QUEUED row with no message
            # would be stuck for good — nothing consumes a status, and this scan
            # only looks at RUNNING. Put it back so the next scan retries it.
            _set_status(db, jid, JobStatus.QUEUED.value, JobStatus.RUNNING.value)
            logger.exception("Job %s: requeue publish failed; will retry next scan.", job_id)
            continue

        # attempt_count is deliberately not touched. The abandoned attempt never
        # finished or failed — it was cut off by something outside the job — so
        # it must not count against the retry budget.
        logger.warning("Job %s abandoned (no heartbeat) — requeued.", job_id)
        reaped.append(job_id)
    return reaped


def liveness_key() -> str:
    # Per container, so each reaper's healthcheck reports on itself rather than
    # passing because some OTHER reaper is still scanning.
    return f"reaper:last_scan:{socket.gethostname()}"


def main() -> None:
    logger.info("Reaper started. Scanning every %ds; eligible after %ds without a heartbeat.",
                SCAN_INTERVAL_SECONDS, REAP_GRACE_SECONDS)
    while True:
        db = SessionLocal()
        try:
            reaped = reap_once(db)
            if reaped:
                logger.info("Reaped %d abandoned job(s).", len(reaped))
            # Written only after a SUCCESSFUL scan, with a TTL of a few scan
            # intervals. `restart: unless-stopped` covers a reaper that exits; it
            # does nothing for one that is still running but hung or failing
            # every scan — crash recovery would silently stop. The Compose
            # healthcheck reads this key, so that state shows up as `unhealthy`.
            redis_client.setex(liveness_key(), SCAN_INTERVAL_SECONDS * 3, str(time.time()))
        except Exception:  # noqa: BLE001 — one bad scan must not end the loop
            logger.exception("Reaper scan failed — will retry next interval.")
        finally:
            db.close()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
