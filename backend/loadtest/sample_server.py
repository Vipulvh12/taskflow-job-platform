"""Records the server side of a load test while Locust records the client side.
From backend/, started just before Locust and running a little longer than it:

    .venv/Scripts/python -m loadtest.sample_server <seconds> <out_prefix>

Writes two CSVs:

  <out_prefix>_probe.csv   t, ms — GET /health every 100 ms over one kept-alive
      connection, from a separate process. /health runs only SELECT 1, but
      through the same path as every request (a threadpool thread, a pooled
      connection), so it separates "the API can't serve anything" from "the
      queries are slow". It also tells server-side stalls from load-generator
      stalls: if Locust records a slow request while the probe was fast, the
      delay was on Locust's side.

  <out_prefix>_server.csv  t, per-container CPU %, Postgres client connections
      (total / active / idle / idle in transaction / waiting on a lock), and
      `jobs` queue depth. "Idle in transaction" means a connection checked out
      by a request that isn't currently running a query.

Docker's CPU % is per core: 100% is one core fully busy, and the ceiling is
100% × the VM's CPU count.
"""
from loadtest import host_env  # noqa: F401  (must precede any app import)

import csv
import http.client
import subprocess
import sys
import threading
import time

import pika
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.queue_topology import QUEUE_JOBS

CONTAINERS = ["api", "postgres", "worker", "redis", "rabbitmq"]
PROBE_INTERVAL = 0.1

PG_SQL = text(
    """
    SELECT count(*),
           count(*) FILTER (WHERE state = 'active'),
           count(*) FILTER (WHERE state = 'idle'),
           count(*) FILTER (WHERE state = 'idle in transaction'),
           count(*) FILTER (WHERE wait_event_type = 'Lock')
    FROM pg_stat_activity
    WHERE datname = current_database() AND backend_type = 'client backend'
      AND pid <> pg_backend_pid()
    """
)


def probe(deadline: float, path: str) -> None:
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["t", "ms"])
        conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
        while time.time() < deadline:
            start = time.time()
            try:
                conn.request("GET", "/health")
                conn.getresponse().read()
                out.writerow([f"{start:.3f}", f"{(time.time() - start) * 1000:.1f}"])
            except (OSError, http.client.HTTPException):
                out.writerow([f"{start:.3f}", "error"])
                conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
            time.sleep(PROBE_INTERVAL)


def docker_cpu() -> dict[str, float]:
    names = [f"taskflow-{c}-1" for c in CONTAINERS]
    lines = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}", *names],
        capture_output=True, text=True,
    ).stdout.split("\n")
    cpu = {}
    for line in lines:
        if line.strip():
            name, pct = line.split()
            cpu[name.removeprefix("taskflow-").removesuffix("-1")] = float(pct.rstrip("%"))
    return cpu


def queue_depth(channel) -> int:
    return channel.queue_declare(queue=QUEUE_JOBS, passive=True).method.message_count


def main():
    seconds, prefix = float(sys.argv[1]), sys.argv[2]
    deadline = time.time() + seconds
    prober = threading.Thread(target=probe, args=(deadline, f"{prefix}_probe.csv"))
    prober.start()

    rabbit = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
    channel = rabbit.channel()
    with engine.connect() as pg, open(f"{prefix}_server.csv", "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["t", *[f"cpu_{c}" for c in CONTAINERS],
                      "pg_total", "pg_active", "pg_idle", "pg_idle_in_tx", "pg_lock_wait",
                      "queue_depth"])
        while time.time() < deadline:
            t = time.time()
            cpu = docker_cpu()  # ~1.5-2s per call; it paces the loop
            pg_row = pg.execute(PG_SQL).one()
            pg.commit()  # end the snapshot, or pg_stat_activity reads stay frozen
            out.writerow([f"{t:.3f}", *[cpu.get(c, "") for c in CONTAINERS],
                          *pg_row, queue_depth(channel)])
            f.flush()
    rabbit.close()
    prober.join()


if __name__ == "__main__":
    main()
