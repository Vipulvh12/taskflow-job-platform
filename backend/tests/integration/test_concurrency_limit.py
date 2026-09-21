"""Regression test for the deadlock found in Phase 17 (app/concurrency_limit.py).

Without the in-flight cap, a burst bigger than (pool connections + threadpool
threads) wedges the process until pool_timeout: every connection held by a
request waiting for a thread, every thread held by a request waiting for a
connection. Here that looks like ~30s requests and a wall of 500s.
"""
import threading
import time

from app.config import settings

ANYIO_DEFAULT_THREADS = 40
BURST = 100


def test_burst_larger_than_pool_plus_threads_does_not_deadlock(client, auth_headers):
    pool = settings.db_pool_size + settings.db_max_overflow
    # The test only means something if the burst can actually wedge the process.
    assert BURST > pool + ANYIO_DEFAULT_THREADS

    barrier = threading.Barrier(BURST)
    results = []
    lock = threading.Lock()

    def one():
        barrier.wait()
        start = time.perf_counter()
        try:
            # Any authenticated request: get_current_user checks out the
            # Session's connection, held from then on across threadpool hops.
            outcome = client.get("/jobs", headers=auth_headers).status_code
        except Exception as exc:
            # TestClient re-raises server errors — a pool timeout lands here
            # rather than as a 500.
            outcome = type(exc).__name__
        with lock:
            results.append((outcome, time.perf_counter() - start))

    threads = [threading.Thread(target=one) for _ in range(BURST)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = sorted({str(outcome) for outcome, _ in results})
    slowest = max(seconds for _, seconds in results)
    assert len(results) == BURST
    assert outcomes == ["200"], f"outcomes {outcomes}"
    # A wedge lasts pool_timeout (30s). Queueing behind the cap costs well
    # under a second here.
    assert slowest < 10, f"slowest request took {slowest:.1f}s"
