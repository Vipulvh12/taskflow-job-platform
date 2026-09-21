"""Postgres-side cost of one GET /jobs for a bench user: the three statements
the request runs, EXPLAIN (ANALYZE) median of 5. Puts the API's CPU cost per
request in proportion. From backend/:

    .venv/Scripts/python -m loadtest.db_cost <out.json>
"""
from loadtest import host_env  # noqa: F401  (must precede any app import)

import json
import statistics
import sys

from sqlalchemy import text

from app.db import engine

EMAIL = "bench-07@taskflow.local"
QUERIES = {
    "user lookup (get_current_user)": "SELECT * FROM users WHERE id = :uid",
    "COUNT(*) for the page total": "SELECT count(*) FROM jobs WHERE user_id = :uid",
    "page of 20 (Phase 14b index)": (
        "SELECT * FROM jobs WHERE user_id = :uid "
        "ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 0"
    ),
}


def main():
    out = {"user": EMAIL, "runs": 5, "queries": {}}
    with engine.connect() as conn:
        uid = conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": EMAIL}).scalar_one()
        out["user_rows"] = conn.execute(
            text("SELECT count(*) FROM jobs WHERE user_id = :uid"), {"uid": uid}
        ).scalar_one()
        for label, sql in QUERIES.items():
            times, plan = [], None
            for _ in range(5):
                plan = conn.execute(
                    text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}"), {"uid": uid}
                ).scalar_one()[0]
                times.append(plan["Execution Time"])
            node = plan["Plan"]
            while node.get("Plans") and node["Node Type"] in ("Limit", "Aggregate"):
                node = node["Plans"][0]
            out["queries"][label] = {
                "execution_ms": round(statistics.median(times), 3),
                "scan": f"{node['Node Type']}" + (f" [{node['Index Name']}]" if "Index Name" in node else ""),
            }
    with open(sys.argv[1], "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
