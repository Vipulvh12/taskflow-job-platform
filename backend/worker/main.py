import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum, auto

import pika

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow.worker")


class Disposition(Enum):
    """What the message itself should become, decided separately from what
    the job row became. Keeping these apart is what lets process_job stay
    free of AMQP concepts and lets on_message own all channel operations."""

    ACK = auto()          # outcome recorded; the message is finished
    RETRY = auto()        # republish onto a delay queue, then ack
    DEAD_LETTER = auto()  # nack(requeue=False) so the broker routes it to the DLQ


def process_job(job_id: str) -> tuple[Disposition, int | None]:
    """Returns (disposition, retry_delay_seconds)."""
    db = SessionLocal()
    try:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.warning("Job %s not found — skipping (row may have been deleted).", job_id)
            return Disposition.ACK, None

        if job.status in (JobStatus.SUCCESS.value, JobStatus.DEAD.value):
            # At-least-once delivery means this message may be a redelivery
            # of a job whose outcome was already committed. Re-running the
            # handler here would violate the idempotent-processing NFR.
            logger.info("Job %s already %s — skipping duplicate delivery.", job_id, job.status)
            return Disposition.ACK, None

        attempt_number = job.attempt_count + 1
        started_at = datetime.now(timezone.utc)
        job.status = JobStatus.RUNNING.value
        job.started_at = started_at
        db.commit()

        handler = get_handler(job.type)
        if handler is None:
            # Retryable on purpose: a type the API accepts but this worker
            # can't run is usually a deploy that hasn't rolled out yet.
            # Exhausting retries parks it in the DLQ, where an admin can
            # re-run it once the handler ships.
            return _fail(db, job, attempt_number, started_at,
                         f"No handler registered for job type '{job.type}'.",
                         permanent=False)

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
    finally:
        db.close()


def _fail(db, job: Job, attempt_number: int, started_at: datetime,
          error: str, permanent: bool) -> tuple[Disposition, int | None]:
    # The attempt row always says FAILED — it records what happened on this
    # try. The JOB's status says what happens next: RETRYING, or terminal.
    _record_attempt(db, job, attempt_number, started_at, JobStatus.FAILED.value, error=error)

    retries_left = attempt_number < settings.job_max_attempts
    if permanent:
        job.status = JobStatus.FAILED.value
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.warning("Job %s failed permanently on attempt %d (no retry): %s",
                       job.id, attempt_number, error)
        return Disposition.ACK, None

    if not retries_left:
        job.status = JobStatus.DEAD.value
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.error("Job %s exhausted %d attempts — dead-lettering: %s",
                     job.id, attempt_number, error)
        return Disposition.DEAD_LETTER, None

    delay = settings.delay_for_attempt(attempt_number)
    job.status = JobStatus.RETRYING.value
    job.completed_at = None  # not finished; only terminal states get this
    db.commit()
    logger.warning("Job %s failed on attempt %d, retrying in %ds: %s",
                   job.id, attempt_number, delay, error)
    return Disposition.RETRY, delay


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


def _schedule_retry(channel, job_id: str, delay: int) -> None:
    """Publish onto the delay queue for `delay`. Its x-message-ttl expires
    the message, and its dead-letter config routes it back to the main
    queue — so the broker, not a sleeping worker, holds the backoff."""
    channel.basic_publish(
        exchange=EXCHANGE_RETRY,
        routing_key=retry_routing_key(delay),
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

    disposition, delay = process_job(job_id)

    if disposition is Disposition.RETRY:
        try:
            _schedule_retry(channel, job_id, delay)
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
    startup rather than discovered one failed job at a time."""
    available = set(registered_types())
    logger.info("Registered handlers: %s", ", ".join(sorted(available)) or "(none)")
    missing = sorted({t.value for t in JOB_PAYLOAD_SCHEMAS} - available)
    if missing:
        logger.warning(
            "No handler for job type(s): %s — jobs of these types will retry, "
            "then dead-letter.", ", ".join(missing),
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
