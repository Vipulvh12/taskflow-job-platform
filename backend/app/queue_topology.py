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
                        │    └─┤ jobs.retry.1    ttl=5s   │
                        ▼      │ jobs.retry.2    ttl=25s  │
                  [ jobs.dlq ] └──────────────────────────┘
                                        ▲
                         worker publishes here, routing key
                         retry.<tier>, to schedule attempt n+1

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


def retry_queue_name(tier: int) -> str:
    """Named by POSITION in the ladder, not by delay. The delay is
    configurable (JOB_RETRY_DELAYS), so a queue called jobs.retry.5s would
    start lying the moment that setting changed — and x-message-ttl is
    immutable after declaration, so the queue would keep its old wait while
    advertising a new one. Tier numbers can't drift out from under the name."""
    return f"jobs.retry.{tier}"


def retry_routing_key(tier: int) -> str:
    return f"retry.{tier}"


def declare_topology(channel, retry_delays: list[int]) -> None:
    """Idempotent. Safe to call on every connection."""
    channel.exchange_declare(exchange=EXCHANGE_DLX, exchange_type="direct", durable=True)
    channel.exchange_declare(exchange=EXCHANGE_RETRY, exchange_type="direct", durable=True)

    # Terminal resting place for jobs whose retries ran out. The worker
    # gets a message here by nacking with requeue=False, which is the
    # broker's own dead-letter path rather than a hand-rolled republish.
    channel.queue_declare(queue=QUEUE_DLQ, durable=True)
    channel.queue_bind(queue=QUEUE_DLQ, exchange=EXCHANGE_DLX, routing_key=DLQ_ROUTING_KEY)

    # !! These arguments are now part of `jobs`'s permanent identity. RabbitMQ
    # queue arguments are immutable after declaration, so ANY future edit here
    # -- adding an argument, changing the DLX name, even reordering to a
    # different value -- makes this declaration inequivalent to the live queue
    # and every declare will fail with PRECONDITION_FAILED until the queue is
    # deleted. Deleting it discards whatever is queued. Change with care.
    channel.queue_declare(
        queue=QUEUE_JOBS,
        durable=True,
        arguments={
            "x-dead-letter-exchange": EXCHANGE_DLX,
            "x-dead-letter-routing-key": DLQ_ROUTING_KEY,
        },
    )

    # Tier N is the delay applied after attempt N fails. Same immutability
    # warning applies: changing JOB_RETRY_DELAYS does NOT retune an existing
    # queue's x-message-ttl — delete the jobs.retry.* queues and let them be
    # redeclared, or the new setting is silently ignored.
    for tier, delay in enumerate(retry_delays, start=1):
        channel.queue_declare(
            queue=retry_queue_name(tier),
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
            queue=retry_queue_name(tier),
            exchange=EXCHANGE_RETRY,
            routing_key=retry_routing_key(tier),
        )
