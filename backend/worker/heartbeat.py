"""Per-job liveness signal, held in Redis with a TTL.

A heartbeat is high-frequency, ephemeral and self-expiring, which is exactly
what Redis's EXPIRE gives for free: if the key is gone, whoever was running the
job has gone silent. No timestamp polling, no Postgres writes nobody reads.

Keyed by JOB, not by worker. The reaper asks "is anyone still running job X?",
so the job id has to be the lookup key: one EXISTS per job. Keying by
`worker:{worker_id}:{job_id}` would force a SCAN with a trailing wildcard, which
walks the entire keyspace — rate limits, idempotency keys, refresh tokens — once
per RUNNING job, every scan. The owning worker's id is kept as the value, for
diagnostics and so a worker only ever deletes its own heartbeat.
"""

from app.redis_client import redis_client

HEARTBEAT_TTL_SECONDS = 15
# Several beats fit inside one TTL, so a worker that misses one or two through
# ordinary jitter is not declared dead. Only sustained silence expires the key.
HEARTBEAT_INTERVAL_SECONDS = 5

_PREFIX = "job_heartbeat:"

# Delete only if the key still belongs to this worker. A plain DEL could remove
# a different worker's heartbeat if the job had already been reaped and
# re-claimed while this one was finishing.
_COMPARE_AND_DELETE = redis_client.register_script(
    """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """
)


def heartbeat_key(job_id: str) -> str:
    return f"{_PREFIX}{job_id}"


def send_heartbeat(worker_id: str, job_id: str) -> None:
    redis_client.setex(heartbeat_key(job_id), HEARTBEAT_TTL_SECONDS, worker_id)


def clear_heartbeat(worker_id: str, job_id: str) -> bool:
    """True if this worker's heartbeat was removed."""
    return bool(_COMPARE_AND_DELETE(keys=[heartbeat_key(job_id)], args=[worker_id]))


def live_heartbeats(job_ids: list[str]) -> dict[str, bool]:
    """One pipelined round trip for any number of jobs."""
    if not job_ids:
        return {}
    pipe = redis_client.pipeline(transaction=False)
    for job_id in job_ids:
        pipe.exists(heartbeat_key(job_id))
    return {job_id: bool(n) for job_id, n in zip(job_ids, pipe.execute())}

