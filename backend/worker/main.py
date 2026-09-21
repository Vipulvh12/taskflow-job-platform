import json
import logging
import socket
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum, auto

import pika
from sqlalchemy import update

from app.config import settings
from app.core.enums import JobStatus
from app.db import SessionLocal
from app.models.job import Job
from app.models.job_attempt import JobAttempt
from app.queue_topology import (
    EXCHANGE_RETRY,
    QUEUE_JOBS,
    declare_topology,
    retry_routing_key,
)
from app.job_types import JOB_PAYLOAD_SCHEMAS
from worker.handlers import load_handlers
from worker.handlers.registry import HandlerError, JobContext, get_handler, registered_types
from worker.heartbeat import HEARTBEAT_INTERVAL_SECONDS, clear_heartbeat, send_heartbeat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow.worker")

# Unique per process. Hostname makes it readable in `redis-cli GET` (it is the
# container id under Compose); the suffix keeps two processes on one host apart.
WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


class Disposition(Enum):
    """What the message itself should become, decided separately from what
    the job row became. Keeping these apart is what lets process_job stay
    free of AMQP concepts and lets on_message own all channel operations."""

    ACK = auto()          # outcome recorded; the message is finished
    RETRY = auto()        # republish onto a delay queue, then ack
    DEAD_LETTER = auto()  # nack(requeue=False) so the broker routes it to the DLQ


def _claim(db, jid: uuid.UUID, started_at: datetime) -> bool:
    """Atomically take ownership of a job: QUEUED/RETRYING -> RUNNING.

    One conditional UPDATE, so exactly one caller can win. This is what makes it
    safe for a job to have two messages in flight — which it now routinely can:
    when a worker dies mid-job, RabbitMQ redelivers its unacked message, AND the
    reaper publishes a fresh one. Two independent recovery paths for the same
    failure would otherwise mean running the job twice (and, for a RETRYING job,
    skipping its backoff and burning an extra attempt). Rather than coordinating
    them, the loser's claim is simply a no-op — the same move as Phase 5's
    idempotency constraint.
    """
    won = db.execute(
        update(Job)
        .where(Job.id == jid,
               Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRYING.value]))
        .values(status=JobStatus.RUNNING.value, started_at=started_at)
        .returning(Job.id)
    ).first()
    db.commit()
    return won is not None


def _log_unclaimable(job: Job) -> None:
    if job.status in (JobStatus.SUCCESS.value, JobStatus.DEAD.value):
        # At-least-once delivery means this may be a redelivery of a job whose
        # outcome was already committed.
        logger.info("Job %s already %s — skipping duplicate delivery.", job.id, job.status)
    elif job.status == JobStatus.RUNNING.value:
        # Either another worker is running it right now, or its worker died and
        # this is the broker's redelivery. Deciding which is the reaper's job:
        # it alone moves RUNNING back to QUEUED, once the heartbeat is gone.
        logger.info("Job %s is RUNNING elsewhere (or abandoned, pending the reaper) — "
                    "skipping this delivery.", job.id)
    else:
        logger.info("Job %s is %s — not claimable, skipping.", job.id, job.status)


def _beat_until_stopped(job_id: str, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            send_heartbeat(WORKER_ID, job_id)
        except Exception:  # noqa: BLE001 — a Redis blip must not kill the job
            logger.warning("Heartbeat for job %s failed; will retry.", job_id, exc_info=True)


def process_job(job_id: str) -> tuple[Disposition, int | None]:
    """Returns (disposition, retry_tier)."""
    db = SessionLocal()
    try:
        jid = uuid.UUID(job_id)
        started_at = datetime.now(timezone.utc)

        if not _claim(db, jid, started_at):
            job = db.get(Job, jid)
            if job is None:
                logger.warning("Job %s not found — skipping (row may have been deleted).",
                               job_id)
            else:
                _log_unclaimable(job)
            return Disposition.ACK, None

        # The job is ours and RUNNING. Heartbeat for as long as that stays true:
        # the first beat goes out synchronously, and the thread is only stopped
        # in the finally below — AFTER the terminal or RETRYING status has been
        # committed. Stopping it earlier would open a window where the row says
        # RUNNING with no heartbeat, which is exactly what the reaper looks for.
        stop = threading.Event()
        try:
            send_heartbeat(WORKER_ID, job_id)
        except Exception:  # noqa: BLE001
            logger.warning("Initial heartbeat for job %s failed.", job_id, exc_info=True)
        beater = threading.Thread(target=_beat_until_stopped, args=(job_id, stop),
                                  name=f"heartbeat-{job_id[:8]}", daemon=True)
        beater.start()
        try:
            return _run_claimed(db, jid, started_at)
        finally:
            stop.set()
            beater.join(timeout=2)
            try:
                clear_heartbeat(WORKER_ID, job_id)
            except Exception:  # noqa: BLE001 — it expires on its own regardless
                logger.warning("Could not clear heartbeat for job %s.", job_id, exc_info=True)
    finally:
        db.close()


def _run_claimed(db, jid: uuid.UUID, started_at: datetime) -> tuple[Disposition, int | None]:
    job = db.get(Job, jid)
    job_id = str(jid)
    # attempt_count only advances when an attempt is recorded, so an attempt that
    # was abandoned mid-flight (worker killed, job reaped) never consumed any of
    # the retry budget — it was never the job's fault.
    attempt_number = job.attempt_count + 1

    handler = get_handler(job.type)
    if handler is None:
        # Permanent: this worker's registry is fixed for its lifetime, so
        # attempts 2 and 3 would look up the same missing key and fail
        # identically. Burning the ladder buys nothing. If the handler is
        # merely undeployed, the job is recoverable from the DLQ once it
        # ships — which is the same remedy, minus three wasted attempts.
        return _fail(db, job, attempt_number, started_at,
                     f"No handler registered for job type '{job.type}'.",
                     permanent=True)

    context = JobContext(
        job_id=job.id,
        attempt_number=attempt_number,
        storage_dir=settings.storage_dir,
    )
    try:
        result = handler(job.payload, context)
    except HandlerError as exc:
        # The handler rejected its own input. The payload can't change,
        # so a retry would fail identically — terminal immediately.
        return _fail(db, job, attempt_number, started_at, str(exc), permanent=True)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any
        # handler failure must still leave the job in a consistent
        # state rather than crashing the whole consume loop.
        return _fail(db, job, attempt_number, started_at,
                     f"{type(exc).__name__}: {exc}", permanent=False)

    _record_attempt(db, job, attempt_number, started_at,
                    JobStatus.SUCCESS.value, result=result)
    job.status = JobStatus.SUCCESS.value
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Job %s succeeded on attempt %d.", job_id, attempt_number)
    return Disposition.ACK, None


def _fail(db, job: Job, attempt_number: int, started_at: datetime,
          error: str, permanent: bool) -> tuple[Disposition, int | None]:
    # The attempt row always says FAILED — it records what happened on this
    # try. The JOB's status says what happens next: RETRYING, or terminal.
    _record_attempt(db, job, attempt_number, started_at, JobStatus.FAILED.value, error=error)

    # The budget is measured from attempt_base, not from zero. It is 0 until an
    # admin retries a DEAD job, at which point it is set to the attempts already
    # spent — so the retried job gets a full fresh ladder while attempt_count,
    # and the numbering in job_attempts, keep counting instead of restarting.
    budget_used = attempt_number - job.attempt_base
    retries_left = budget_used < settings.job_max_attempts
    if permanent:
        # DEAD, not FAILED. FAILED is reserved for a job that never ran at
        # all — the Phase 6 case where the row committed but the publish
        # failed. Anything that ran and died rests at DEAD, so an admin has
        # exactly one status (and one queue) to look at for failed work.
        job.status = JobStatus.DEAD.value
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.warning("Job %s DEAD (permanent failure) on attempt %d, no retry: %s",
                       job.id, attempt_number, error)
        return Disposition.DEAD_LETTER, None

    if not retries_left:
        job.status = JobStatus.DEAD.value
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error("Job %s exhausted %d attempts — dead-lettering: %s",
                     job.id, attempt_number, error)
        return Disposition.DEAD_LETTER, None

    tier = settings.tier_for_attempt(budget_used)
    job.status = JobStatus.RETRYING.value
    job.completed_at = None  # not finished; only terminal states get this
    db.commit()
    logger.warning("Job %s failed on attempt %d, retrying via tier %d (~%ds): %s",
                   job.id, attempt_number, tier, settings.delay_for_attempt(budget_used),
                   error)
    return Disposition.RETRY, tier


def _record_attempt(db, job: Job, attempt_number: int, started_at: datetime,
                    status: str, result: dict | None = None, error: str | None = None) -> None:
    job.attempt_count = attempt_number
    if result is not None:
        job.result = result
    db.add(JobAttempt(
        job_id=job.id,
        attempt_number=attempt_number,
        status=status,
        error=error,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
    ))


def _schedule_retry(channel, job_id: str, tier: int) -> None:
    """Publish onto the tier's delay queue. Its x-message-ttl expires the
    message, and its dead-letter config routes it back to the main queue —
    so the broker, not a sleeping worker, holds the backoff."""
    channel.basic_publish(
        exchange=EXCHANGE_RETRY,
        routing_key=retry_routing_key(tier),
        body=json.dumps({"job_id": job_id}),
        properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
    )


def on_message(channel, method, properties, body):
    try:
        job_id = json.loads(body)["job_id"]
        uuid.UUID(job_id)  # validate shape before touching the DB
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.error("Malformed message, dropping without requeue: %s", exc)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    disposition, retry_tier = process_job(job_id)

    if disposition is Disposition.RETRY:
        try:
            _schedule_retry(channel, job_id, retry_tier)
        except Exception:
            # Could not schedule the retry. Requeue instead of acking, so
            # the job isn't stranded at RETRYING with no message anywhere.
            # Costs one extra attempt if it comes back — acceptable under
            # at-least-once, and far better than a silently lost job.
            logger.exception("Failed to schedule retry for job %s; requeueing.", job_id)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    if disposition is Disposition.DEAD_LETTER:
        # requeue=False routes through the main queue's x-dead-letter-exchange
        # into jobs.dlq, carrying x-death headers with the full routing history.
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    channel.basic_ack(delivery_tag=method.delivery_tag)


def _log_handler_coverage() -> None:
    """A job type the API accepts but this worker can't run is a real
    operational condition (a partial rollout), and it should be visible at
    startup rather than discovered one dead job at a time — the more so now
    that a missing handler kills a job on its first attempt."""
    available = set(registered_types())
    logger.info("Registered handlers: %s", ", ".join(sorted(available)) or "(none)")
    missing = sorted({t.value for t in JOB_PAYLOAD_SCHEMAS} - available)
    if missing:
        logger.warning(
            "No handler for job type(s): %s — jobs of these types will go "
            "straight to DEAD on their first attempt.", ", ".join(missing),
        )


def main():
    load_handlers()
    _log_handler_coverage()
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = connection.channel()
    declare_topology(channel, settings.retry_delays)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=QUEUE_JOBS, on_message_callback=on_message)

    logger.info("Worker started. max_attempts=%d, backoff=%ss. Waiting for jobs on '%s'...",
                settings.job_max_attempts, settings.retry_delays, QUEUE_JOBS)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
        connection.close()


if __name__ == "__main__":
    main()
