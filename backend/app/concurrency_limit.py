"""Caps in-flight requests per API process at the size of its DB connection pool.

Why: a deadlock found by the Phase 17 load test, and reproducible on demand.

FastAPI runs each sync dependency, the sync endpoint, and response validation
as *separate* calls into one shared threadpool (anyio's default: 40 threads).
A request's Session checks out its connection in get_current_user and keeps it
until the request finishes — across all of those calls. So once more than
(pool connections + threads) requests are in flight, the process can wedge:
every connection is held by a request waiting for a free thread, and every
thread is held by a request waiting for a free connection. Nothing moves until
pool_timeout (30s) fails the threads that are waiting on the pool — 40 HTTP
500s, one per thread — and the backlog drains.

Measured: 50 simultaneous requests pass and 60 wedge (15 connections + 40
threads = 55). Raising the pool to 40 only moves the threshold, to 80 — 100
simultaneous requests still wedge the same way.

Capping in-flight requests at the pool size removes the cycle rather than
raising the threshold: at most that many requests hold or want a connection,
so a checkout never waits, and they can never need more threads than exist.
Requests over the cap wait here, in the event loop, holding nothing. Each
request uses exactly one Session (get_db) — that's what makes this sufficient.
"""
import asyncio


class ConcurrencyLimitMiddleware:
    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit
        self._loop = None
        self._semaphore = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        # One per event loop. Uvicorn has exactly one, but each TestClient runs
        # its own, and an asyncio primitive can't be shared across loops.
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop, self._semaphore = loop, asyncio.Semaphore(self.limit)
        return self._semaphore

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Held until the response is fully sent, which is after get_db's
        # teardown has closed the Session and returned its connection.
        async with self._get_semaphore():
            await self.app(scope, receive, send)
