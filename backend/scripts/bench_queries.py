"""Phase 14 benchmark harness. Runs EXPLAIN (ANALYZE, BUFFERS) against the
exact SQL the API emits, several times per query, and records the plan shape,
execution time and buffer usage.

EXPLAIN ANALYZE, not wall-clock timing from a client: client timing bundles
network, connection setup and — as Phase 5 found — a ~2s localhost/IPv6 stall
that has nothing to do with the database. Execution Time is measured inside
Postgres and isolates exactly what an index changes.

    python scripts/bench_queries.py <label> <user_id> [<user_id> ...] > out.json
"""
import json
import os
import statistics
import sys

import psycopg2

DSN = "postgresql://taskflow:taskflow_dev_password@localhost:5433/taskflow"
RUNS = 7  # first is reported separately as "cold"; median of the rest is "warm"

# Copied verbatim from what job_service.list_jobs compiles to (Phase 14 captured
# it from SQLAlchemy directly) — benchmarking a simplified query would measure
# something the API never actually runs.
LIST_COLUMNS = (
    "jobs.id, jobs.user_id, jobs.type, jobs.status, jobs.priority, jobs.payload, "
    "jobs.result, jobs.idempotency_key, jobs.attempt_count, jobs.created_at, "
    "jobs.started_at, jobs.completed_at"
)
QUERIES = {
    # GET /jobs — the default page. JobList polls this every 2s while active.
    "list_default": (
        f"SELECT {LIST_COLUMNS} FROM jobs WHERE jobs.user_id = %(uid)s "
        "ORDER BY jobs.created_at DESC, jobs.id DESC LIMIT 20 OFFSET 0"
    ),
    # GET /jobs?status=QUEUED
    "list_filtered": (
        f"SELECT {LIST_COLUMNS} FROM jobs WHERE jobs.user_id = %(uid)s "
        "AND jobs.status = 'QUEUED' ORDER BY jobs.created_at DESC, jobs.id DESC "
        "LIMIT 20 OFFSET 0"
    ),
    # The `total` that accompanies every filtered list response.
    "count_filtered": (
        f"SELECT count(*) AS count_1 FROM (SELECT {LIST_COLUMNS} FROM jobs "
        "WHERE jobs.user_id = %(uid)s AND jobs.status = 'QUEUED') AS anon_1"
    ),
    # Highest-priority queued work, across all users (admin / scheduler shape).
    "priority_queue": (
        "SELECT id FROM jobs WHERE status = 'QUEUED' ORDER BY priority DESC LIMIT 50"
    ),
}


def node_chain(plan):
    chain = []
    node = plan
    while node:
        name = node["Node Type"]
        if node.get("Index Name"):
            name += f" [{node['Index Name']}]"
        chain.append(name)
        node = (node.get("Plans") or [None])[0]
    return " > ".join(chain)


def totals(plan):
    """Buffers summed over the whole tree are already rolled up at the root."""
    return plan.get("Shared Hit Blocks", 0), plan.get("Shared Read Blocks", 0)


def run(cur, sql, params):
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
    return cur.fetchone()[0][0]


def main():
    label, user_ids = sys.argv[1], sys.argv[2:]
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    if os.environ.get("BENCH_FORCE_SEQSCAN") == "1":
        # Postgres has no "ignore this one index" hint, but it can disable index
        # access paths for a session. This is the only honest way to get a
        # true no-index baseline here: dropping idx_jobs_user_status is not
        # enough, because uq_jobs_user_idempotency_key (user_id leading) still
        # serves `WHERE user_id = ?`.
        for gate in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
            cur.execute(f"SET {gate} = off")
    cur.execute("SELECT count(*) FROM jobs")
    table_rows = cur.fetchone()[0]

    out = {"label": label, "table_rows": table_rows, "results": []}
    for uid in user_ids:
        cur.execute("SELECT count(*) FROM jobs WHERE user_id = %s", (uid,))
        user_rows = cur.fetchone()[0]
        for name, sql in QUERIES.items():
            if name == "priority_queue" and uid != user_ids[0]:
                continue  # not per-user; measure once
            runs = [run(cur, sql, {"uid": uid}) for _ in range(RUNS)]
            warm = runs[1:]
            last = warm[-1]
            hit, read = totals(last["Plan"])
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, {"uid": uid})
            text_plan = "\n".join(r[0] for r in cur.fetchall())
            out["results"].append({
                "query": name,
                "user_id": uid if name != "priority_queue" else None,
                "user_rows": user_rows if name != "priority_queue" else None,
                "plan": node_chain(last["Plan"]),
                "cold_ms": round(runs[0]["Execution Time"], 3),
                "warm_median_ms": round(statistics.median(r["Execution Time"] for r in warm), 3),
                "warm_min_ms": round(min(r["Execution Time"] for r in warm), 3),
                "planning_ms": round(statistics.median(r["Planning Time"] for r in warm), 3),
                "rows_returned": last["Plan"].get("Actual Rows"),
                "shared_hit": hit,
                "shared_read": read,
                "text_plan": text_plan,
            })
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
