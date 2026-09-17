import json
import logging
import threading

import pika
from pika.exceptions import AMQPError

from app.config import settings

logger = logging.getLogger("taskflow")

QUEUE_NAME = "jobs"

_lock = threading.Lock()
_connection: pika.BlockingConnection | None = None
_channel = None


def _get_channel():
    global _connection, _channel
    if _connection is None or _connection.is_closed:
        _connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
        _channel = _connection.channel()
        # idempotent: no-op if the queue already exists with matching args
        _channel.queue_declare(queue=QUEUE_NAME, durable=True)
    return _channel


def publish_job(job_id: str) -> None:
    """Publishes a minimal trigger message — just the job_id. Postgres,
    not the queue, is the source of truth for job data; the worker
    (Phase 7) re-fetches the full row by id rather than trusting
    anything carried in the message.

    Raises pika.exceptions.AMQPError on any connection/publish failure —
    callers must handle this explicitly; it is not swallowed here."""
    global _connection, _channel
    message = json.dumps({"job_id": job_id})
    properties = pika.BasicProperties(
        delivery_mode=2,  # persist message to disk
        content_type="application/json",
    )
    with _lock:
        # Two attempts, because a cached connection can be dead without
        # knowing it: pika only discovers a broker that went away when it
        # next touches the socket, so is_closed still reads False and
        # _get_channel() hands back a stale channel. Without the retry, the
        # first job submitted after any broker restart is sacrificed to
        # discovering that — a 502 and a FAILED row for an outage the user
        # never saw. The retry makes a broker restart invisible instead.
        for attempt in (1, 2):
            try:
                channel = _get_channel()
                channel.basic_publish(
                    exchange="",
                    routing_key=QUEUE_NAME,
                    body=message,
                    properties=properties,
                )
                return
            except AMQPError:
                # Drop both refs so the next _get_channel() rebuilds from
                # scratch rather than reusing an object we know is broken.
                _connection = None
                _channel = None
                if attempt == 2:
                    raise
                logger.warning(
                    "RabbitMQ publish failed on a cached connection; "
                    "reconnecting and retrying once."
                )
