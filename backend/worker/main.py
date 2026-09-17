import json
import logging
import uuid
from datetime import datetime, timezone

import pika

from app.config import settings
from app.core.enums import JobStatus
from app.db import SessionLocal
from app.models.job import Job
from app.models.job_attempt import JobAttempt
from app.rabbitmq_client import QUEUE_NAME
from worker.handlers import csv_process  # noqa: F401 — import registers the handler
from worker.handlers.registry import get_handler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow.worker")


def process_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.warning("Job %s not found — skipping (row may have been deleted).", job_id)
            return

        if job.status in (JobStatus.SUCCESS.value, JobStatus.DEAD.value):
            # At-least-once delivery means this message may be a redelivery
            # of a job whose outcome was already committed. Re-running the
            # handler here would violate the idempotent-processing NFR.
            logger.info("Job %s already %s — skipping duplicate delivery.", job_id, job.status)
            return

        attempt_number = job.attempt_count + 1
        started_at = datetime.now(timezone.utc)
        job.status = JobStatus.RUNNING.value
        job.started_at = started_at
        db.commit()

        handler = get_handler(job.type)
        if handler is None:
            _finish(db, job, attempt_number, started_at, JobStatus.FAILED.value,
                    error=f"No handler registered for job type '{job.type}'.")
            return

        try:
            result = handler(job.payload)
        except Exception as exc:  # noqa: BLE001 — deliberately broad: any
            # handler failure, expected or not, must still leave the job
            # in a consistent terminal state rather than crashing the
            # whole consume loop over one bad job.
            _finish(db, job, attempt_number, started_at, JobStatus.FAILED.value, error=str(exc))
            return

        _finish(db, job, attempt_number, started_at, JobStatus.SUCCESS.value, result=result)
        logger.info("Job %s succeeded on attempt %d.", job_id, attempt_number)
    finally:
        db.close()


def _finish(db, job: Job, attempt_number: int, started_at: datetime,
            status: str, result: dict | None = None, error: str | None = None) -> None:
    completed_at = datetime.now(timezone.utc)
    job.status = status
    job.attempt_count = attempt_number
    job.completed_at = completed_at
    if result is not None:
        job.result = result
    db.add(JobAttempt(
        job_id=job.id,
        attempt_number=attempt_number,
        status=status,
        error=error,
        started_at=started_at,
        completed_at=completed_at,
    ))
    db.commit()
    if status == JobStatus.FAILED.value:
        logger.warning("Job %s failed on attempt %d: %s", job.id, attempt_number, error)


def on_message(channel, method, properties, body):
    try:
        job_id = json.loads(body)["job_id"]
        uuid.UUID(job_id)  # validate shape before touching the DB
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.error("Malformed message, dropping without requeue: %s", exc)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        process_job(job_id)
    finally:
        # Ack unconditionally: SUCCESS and FAILED are both outcomes already
        # committed to Postgres. A redelivery is only useful if the worker
        # PROCESS crashes before this line — and then we never get here,
        # so the broker's own requeue-on-disconnect covers that case.
        channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE_NAME, durable=True)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=on_message)

    logger.info("Worker started. Waiting for jobs on '%s'...", QUEUE_NAME)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
        connection.close()


if __name__ == "__main__":
    main()
