"""Fires N simultaneous GET /jobs requests — the on-demand reproduction of the
deadlock app/concurrency_limit.py fixes. From backend/:

    .venv/Scripts/python -m loadtest.burst <n> [port] [out.json]

Each request gets its own connection, opened in advance, and all are released
together by a barrier, so the server sees one burst of n requests. Meanwhile it
samples pg_stat_activity every 0.5s. Every pooled connection "idle in
transaction" with none active means every connection is checked out and none
is running a query. For one sample that's just a full pool; held for
pool_timeout it's the wedge.

Without the fix, n above (pool connections + 40 threadpool threads) wedges the
process for pool_timeout (30s) and 40 requests fail with 500.
"""
from loadtest import host_env  # noqa: F401  (must precede any app import)

import http.client
import json
import sys
import threading
import time

from sqlalchemy import text

from app.db import engine
from loadtest.locustfile import _tokens_for

STATES_SQL = text(
    """
    SELECT coalesce(state, 'unknown'), count(*) FROM pg_stat_activity
    WHERE datname = current_database() AND backend_type = 'client backend'
      AND pid <> pg_backend_pid()
    GROUP BY 1
    """
)


def burst(n: int, port: int = 8000) -> dict:
    tokens = _tokens_for("bench-%@taskflow.local")
    barrier = threading.Barrier(n)
    results, lock = [], threading.Lock()

    def one(i):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        conn.connect()
        barrier.wait()
        start = time.perf_counter()
        try:
            conn.request("GET", "/jobs?page=1&page_size=20",
                         headers={"Authorization": f"Bearer {tokens[i % len(tokens)]}"})
            resp = conn.getresponse()
            resp.read()
            outcome = str(resp.status)
        except (OSError, http.client.HTTPException) as exc:
            outcome = type(exc).__name__
        with lock:
            results.append((outcome, time.perf_counter() - start))

    done, samples = threading.Event(), []

    def sample():
        with engine.connect() as pg:
            while not done.is_set():
                samples.append(dict(pg.execute(STATES_SQL).all()))
                pg.commit()
                time.sleep(0.5)

    sampler = threading.Thread(target=sample)
    sampler.start()
    workers = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    done.set()
    sampler.join()

    latencies = sorted(seconds for _, seconds in results)
    outcomes = {}
    for outcome, _ in results:
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "n": n,
        "outcomes": outcomes,
        "p50_ms": round(latencies[len(latencies) // 2] * 1000),
        "max_ms": round(latencies[-1] * 1000),
        "peak_idle_in_transaction": max(s.get("idle in transaction", 0) for s in samples),
        # A single one is a momentarily full pool; a wedge fills every sample.
        "full_pool_idle_samples": sum(
            1 for s in samples if s.get("idle in transaction", 0) >= 15 and not s.get("active")
        ),
        "samples": len(samples),
    }


if __name__ == "__main__":
    n = int(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    result = burst(n, port)
    print(json.dumps(result))
    if len(sys.argv) > 3:
        with open(sys.argv[3], "a") as f:
            f.write(json.dumps(result) + "\n")
