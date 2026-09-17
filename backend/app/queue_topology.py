"""Single source of truth for the RabbitMQ topology.

Both the API (publisher) and the worker (consumer) declare these. RabbitMQ
requires every declaration of a queue to pass identical arguments, so two
modules declaring the same queue with drifting arguments is a
PRECONDITION_FAILED waiting to happen — hence one function both call.

    (api) ──publish──> [ jobs ] <──consume── (worker)
                          │  ▲
         nack(requeue=    │  │  TTL expiry dead-letters back
          False) on       │  │  to the default exchange with
         exhausted        │  │  routing key "jobs"
          retries         ▼  │
                  [jobs.dlx] │ ┌──────────────────────────┐
                        │    └─┤ jobs.retry.5s   ttl=5s   │
                        ▼      │ jobs.retry.25s  ttl=25s  │
                  [ jobs.dlq ] └──────────────────────────┘
                                        ▲
                         worker publishes here, routing key
                         retry.<delay>, to schedule attempt n+1

Why one queue per delay instead of one retry queue with a per-message
TTL: RabbitMQ only expires messages at the HEAD of a queue. A message
with a 5s TTL sitting behind one with a 25s TTL waits the full 25s. Per
-queue TTL means every message in a given queue shares a deadline, so
FIFO expiry is exactly right.
"""

QUEUE_JOBS = "jobs"
QUEUE_DLQ = "jobs.dlq"
EXCHANGE_DLX = "jobs.dlx"
EXCHANGE_RETRY = "jobs.retry"
DLQ_ROUTING_KEY = "dead"


def retry_queue_name(delay_seconds: int) -> str:
    return f"jobs.retry.{delay_seconds}s"


def retry_routing_key(delay_seconds: int) -> str:
    return f"retry.{delay_seconds}"


def declare_topology(channel, retry_delays: list[int]) -> None:
    """Idempotent. Safe to call on every connection."""
    channel.exchange_declare(exchange=EXCHANGE_DLX, exchange_type="direct", durable=True)
    channel.exchange_declare(exchange=EXCHANGE_RETRY, exchange_type="direct", durable=True)

    # Terminal resting place for jobs whose retries ran out. The worker
    # gets a message here by nacking with requeue=False, which is the
    # broker's own dead-letter path rather than a hand-rolled republish.
    channel.queue_declare(queue=QUEUE_DLQ, durable=True)
    channel.queue_bind(queue=QUEUE_DLQ, exchange=EXCHANGE_DLX, routing_key=DLQ_ROUTING_KEY)

    channel.queue_declare(
        queue=QUEUE_JOBS,
        durable=True,
        arguments={
            "x-dead-letter-exchange": EXCHANGE_DLX,
            "x-dead-letter-routing-key": DLQ_ROUTING_KEY,
        },
    )

    for delay in retry_delays:
        channel.queue_declare(
            queue=retry_queue_name(delay),
            durable=True,
            arguments={
                "x-message-ttl": delay * 1000,
                # Empty exchange = the default exchange, which routes by
                # queue name — so an expired message lands straight back
                # in the main queue with no extra exchange to declare.
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": QUEUE_JOBS,
            },
        )
        channel.queue_bind(
            queue=retry_queue_name(delay),
            exchange=EXCHANGE_RETRY,
            routing_key=retry_routing_key(delay),
        )
