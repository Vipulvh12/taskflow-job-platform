"""Queue delay for the jobs a load test submitted: how long each waited between
the API accepting it and the worker starting it. Locust can't see this — its
clock stops when POST /jobs returns 201. From backend/:

    .venv/Scripts/python -m loadtest.queue_delay mark              # before the run
    .venv/Scripts/python -m loadtest.queue_delay report <mark> <out.json>

`mark` prints Postgres's now(), so the window is on the database's clock (the
one created_at uses) rather than the host's. `report` waits for the worker to
drain everything submitted since then, and records how long that took.

queue_delay = started_at - created_at   (waiting in RabbitMQ)
end_to_end  = completed_at - created_at (accepted -> result written)
"""
from loadtest import host_env  # noqa: F401  (must precede any app import)

import json
import sys
import time

from sqlalchemy import text

from app.db import engine

DRAIN_TIMEOUT_SECONDS = 600

LOADTEST_JOBS = """
    FROM jobs
    WHERE created_at > CAST(:mark AS timestamptz)
      AND user_id IN (SELECT id FROM users WHERE email LIKE 'loadtest-%@taskflow.local')
"""

PENDING_SQL = text(f"SELECT count(*) {LOADTEST_JOBS} AND status IN ('QUEUED', 'RUNNING', 'RETRYING')")
STATUS_SQL = text(f"SELECT status, count(*) {LOADTEST_JOBS} GROUP BY status ORDER BY status")
DELAY_SQL = text(
    f"""
    SELECT
      count(*),
      percentile_cont(0.50) WITHIN GROUP (ORDER BY qd), percentile_cont(0.95) WITHIN GROUP (ORDER BY qd),
      percentile_cont(0.99) WITHIN GROUP (ORDER BY qd), max(qd),
      percentile_cont(0.50) WITHIN GROUP (ORDER BY e2e), percentile_cont(0.95) WITHIN GROUP (ORDER BY e2e),
      percentile_cont(0.99) WITHIN GROUP (ORDER BY e2e), max(e2e),
      min(created_at), max(created_at), max(completed_at)
    FROM (
      SELECT created_at, completed_at,
             extract(epoch FROM started_at - created_at) * 1000 AS qd,
             extract(epoch FROM completed_at - created_at) * 1000 AS e2e
      {LOADTEST_JOBS} AND status = 'SUCCESS'
    ) s
    """
)


def mark():
    with engine.connect() as conn:
        print(conn.execute(text("SELECT now()")).scalar_one().isoformat())


def report(mark_ts: str, out_path: str):
    waited = 0.0
    with engine.connect() as conn:
        while True:
            pending = conn.execute(PENDING_SQL, {"mark": mark_ts}).scalar_one()
            conn.commit()
            if pending == 0 or waited >= DRAIN_TIMEOUT_SECONDS:
                break
            time.sleep(1)
            waited += 1
        statuses = dict(conn.execute(STATUS_SQL, {"mark": mark_ts}).all())
        row = conn.execute(DELAY_SQL, {"mark": mark_ts}).one()

    def ms(v):
        return None if v is None else round(float(v), 1)

    submitted_span = (row[10] - row[9]).total_seconds() if row[9] else None
    result = {
        "statuses": statuses,
        "still_pending": pending,
        "waited_for_drain_s": waited,
        "succeeded": row[0],
        "queue_delay_ms": {"p50": ms(row[1]), "p95": ms(row[2]), "p99": ms(row[3]), "max": ms(row[4])},
        "end_to_end_ms": {"p50": ms(row[5]), "p95": ms(row[6]), "p99": ms(row[7]), "max": ms(row[8])},
        # Submission rate vs completion rate: if the worker kept up, the last
        # job finished moments after the last one was submitted.
        "submitted_over_s": submitted_span,
        "last_completed_after_last_submitted_s": (
            (row[11] - row[10]).total_seconds() if row[11] and row[10] else None
        ),
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "mark":
        mark()
    else:
        report(sys.argv[2], sys.argv[3])
