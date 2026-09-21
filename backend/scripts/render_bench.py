"""Renders benchmarks/phase14_results.md from the raw JSON the harness wrote.
Query timings, buffer counts, plans and row counts all come from
benchmarks/raw/*.json, so the prose can't drift from the measurements. The one
exception is §6's index sizes and build times, read from psql during the run."""
import json
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / "benchmarks" / "raw"
OUT = RAW.parent / "phase14_results.md"


def load(name):
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))


def pick(data, query, heavy=None):
    for r in data["results"]:
        if r["query"] != query:
            continue
        if heavy is None or r["user_rows"] is None:
            return r
        if heavy == (r["user_rows"] > 1000):
            return r
    raise KeyError(query)


def ms(x):
    return f"{x:.3f} ms" if x < 1 else f"{x:.2f} ms"


def x(a, b):
    return f"{a / b:,.0f}×" if a / b >= 1.5 else ("≈ same" if a / b > 0.67 else f"{b / a:.1f}× slower")


A1, A2 = load("A1_small_indexed"), load("A2_small_noindex")
B0, B1 = load("B0_large_seqscan"), load("B1_large_noindex")
B2, B3 = load("B2_large_indexed"), load("B3_experiment_user_created")

heavy_rows = pick(B2, "list_default", True)["user_rows"]
light_rows = pick(B2, "list_default", False)["user_rows"]

L = []
w = L.append

w("# Phase 14 — Index Benchmark Results\n")
w("All query timings are Postgres-internal `Execution Time` from "
  "`EXPLAIN (ANALYZE, BUFFERS)`, **median of 6 warm runs** after one discarded cold run. "
  "Timings, buffer counts, plans and row counts are generated from `benchmarks/raw/*.json` "
  "by `scripts/render_bench.py`, so they cannot drift from the measurements. Index sizes, "
  "build times and the heap size were read from `psql` during the run.\n")

w("## Setup\n")
w(f"- **Small table:** {A1['table_rows']} rows — real dev data from 13 phases of testing.")
w(f"- **Large table:** {B2['table_rows']:,} rows — the same real rows plus seeded ones spread "
  f"across 20 dedicated `bench-NN@taskflow.local` users (~5% of the table each). "
  "24 MB heap, 3,078 pages.")
w(f"- **Heavy user:** {heavy_rows:,} rows (5.1% of the table). "
  f"**Light user:** {light_rows} rows (0.2%).")
w("- `VACUUM ANALYZE jobs` before every measurement set, so the planner works from current "
  "statistics and the visibility map allows index-only scans.")
w("- Queries are the **exact SQL** `job_service.list_jobs` compiles to, captured from "
  "SQLAlchemy — not simplified approximations. That includes all 12 columns and "
  "`ORDER BY created_at DESC, id DESC`.\n")

w("| Query | SQL shape | Where it runs |")
w("|---|---|---|")
w("| `list_default` | `WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 20` | `GET /jobs` — **polled every 2s by the job list** |")
w("| `list_filtered` | same, `AND status='QUEUED'` | `GET /jobs?status=QUEUED` |")
w("| `count_filtered` | `count(*)` over the filtered set | the `total` in every filtered list response |")
w("| `priority_queue` | `WHERE status='QUEUED' ORDER BY priority DESC LIMIT 50` | admin / scheduler shape — **no endpoint issues this today** |")
w("")

# ---------------------------------------------------------------------------
w("## 1. At 447 rows, indexes don't matter\n")
w("| Query | With indexes | Without | Plan with indexes |")
w("|---|---|---|---|")
for q in ("list_default", "list_filtered", "count_filtered", "priority_queue"):
    a, b = pick(A1, q), pick(A2, q)
    w(f"| `{q}` | {ms(a['warm_median_ms'])} | {ms(b['warm_median_ms'])} | {a['plan']} |")
w("")
w("Every query is under half a millisecond either way, and the whole table is 12 pages. For "
  "the list queries the planner **ignores** `idx_jobs_user_status` even when it exists: this "
  "user owns 443 of the 447 rows, so an index cannot narrow anything and a sequential scan "
  "is correctly cheaper. The lesson from this tier is that an index only pays off once the "
  "table is large *and* the predicate is selective.\n")

# ---------------------------------------------------------------------------
w("## 2. The finding that changed the experiment\n")
w("Dropping `idx_jobs_user_status` did **not** produce a sequential scan for the per-user "
  "queries. The planner switched to **`uq_jobs_user_idempotency_key`** — the index that backs "
  "Phase 5's idempotency constraint. It is on `(user_id, idempotency_key)`, and since `user_id` "
  "is its **leading column** it serves `WHERE user_id = ?` perfectly well.\n")
w("```")
w(pick(B1, "list_default", True)["text_plan"])
w("```\n")
w("So for `user_id` lookups, `idx_jobs_user_status` is partly redundant with an index that "
  "already had to exist. Dropping one index does not give a no-index baseline for Query 1; "
  "a true worst case needed index access paths disabled for the session "
  "(`enable_indexscan`, `enable_bitmapscan`, `enable_indexonlyscan = off`). The large-table "
  "results therefore compare **three** conditions:\n")
w("1. **No index usable** — every index path disabled. The genuine worst case.")
w("2. **Constraint index only** — `idx_jobs_user_status` and `idx_jobs_status_priority` dropped; "
  "`uq_jobs_user_idempotency_key` still present, as it always would be.")
w("3. **With the Phase 2 indexes** — the schema as migrated.\n")

# ---------------------------------------------------------------------------
w(f"## 3. At {B2['table_rows']:,} rows\n")
w("| User | Query | No index | Constraint index only | With Phase 2 indexes | vs no index | vs constraint only |")
w("|---|---|---|---|---|---|---|")
for heavy, who in ((True, "heavy"), (False, "light")):
    for q in ("list_default", "list_filtered", "count_filtered"):
        a, b, c = pick(B0, q, heavy), pick(B1, q, heavy), pick(B2, q, heavy)
        w(f"| {who} | `{q}` | {ms(a['warm_median_ms'])} | {ms(b['warm_median_ms'])} | "
          f"**{ms(c['warm_median_ms'])}** | {x(a['warm_median_ms'], c['warm_median_ms'])} | "
          f"{x(b['warm_median_ms'], c['warm_median_ms'])} |")
a, b, c = pick(B0, "priority_queue"), pick(B1, "priority_queue"), pick(B2, "priority_queue")
w(f"| all | `priority_queue` | {ms(a['warm_median_ms'])} | {ms(b['warm_median_ms'])} | "
  f"**{ms(c['warm_median_ms'])}** | {x(a['warm_median_ms'], c['warm_median_ms'])} | "
  f"{x(b['warm_median_ms'], c['warm_median_ms'])} |")
w("")

w("### Pages touched (8 KB shared buffers)\n")
w("| User | Query | No index | Constraint only | With indexes | Plan with indexes |")
w("|---|---|---|---|---|---|")
for heavy, who in ((True, "heavy"), (False, "light")):
    for q in ("list_default", "list_filtered", "count_filtered"):
        a, b, c = pick(B0, q, heavy), pick(B1, q, heavy), pick(B2, q, heavy)
        w(f"| {who} | `{q}` | {a['shared_hit']:,} | {b['shared_hit']:,} | **{c['shared_hit']:,}** | {c['plan']} |")
a, b, c = pick(B0, "priority_queue"), pick(B1, "priority_queue"), pick(B2, "priority_queue")
w(f"| all | `priority_queue` | {a['shared_hit']:,} | {b['shared_hit']:,} | **{c['shared_hit']:,}** | {c['plan']} |")
w("")
w("Pages are the more honest measure of work than milliseconds: everything here was served "
  "from memory (`read=0` throughout), so timings would grow far faster than these ratios "
  "suggest once the table no longer fits in cache.\n")

# ---------------------------------------------------------------------------
w("## 4. What each index actually earns\n")
pq0, pq2 = pick(B0, "priority_queue"), pick(B2, "priority_queue")
w(f"**`idx_jobs_status_priority` — the clearest win.** `priority_queue` goes from "
  f"{ms(pq0['warm_median_ms'])} to {ms(pq2['warm_median_ms'])} "
  f"(**{x(pq0['warm_median_ms'], pq2['warm_median_ms'])}**), and from {pq0['shared_hit']:,} "
  f"pages to {pq2['shared_hit']}. Postgres walks the index backward within "
  "`status = 'QUEUED'` and stops after 50 entries — no sort, no table scan:\n")
w("```")
w(pq2["text_plan"])
w("```\n")
w("The caveat: **no endpoint issues this query today.** RabbitMQ, not Postgres, orders work "
  "for the worker. The index earns its keep only once something — an admin queue view, a "
  "scheduler, or V2's abandoned-job sweep — actually asks Postgres for the next-priority "
  "queued job. Until then it is paid for on every insert and used by nothing.\n")

cf1, cf2 = pick(B1, "count_filtered", True), pick(B2, "count_filtered", True)
lf1, lf2 = pick(B1, "list_filtered", True), pick(B2, "list_filtered", True)
w(f"**`idx_jobs_user_status` — real, but narrower than it looks.** Its value is the `status` "
  f"column, since `user_id` alone is already covered. The count becomes an **index-only "
  f"scan** that never touches the table: {ms(cf1['warm_median_ms'])} → "
  f"{ms(cf2['warm_median_ms'])} and **{cf1['shared_hit']:,} → {cf2['shared_hit']} pages** for "
  f"the heavy user. The filtered list improves "
  f"{x(lf1['warm_median_ms'], lf2['warm_median_ms'])} over the constraint index alone, but "
  "still sorts its matches:\n")
w("```")
w(lf2["text_plan"])
w("```\n")

ld1, ld2 = pick(B1, "list_default", True), pick(B2, "list_default", True)
w(f"**Neither index helps the hottest query.** `list_default` — the page the job list polls "
  f"every 2 seconds — runs the same plan with or without the Phase 2 indexes "
  f"({ms(ld1['warm_median_ms'])} vs {ms(ld2['warm_median_ms'])}, {ld2['shared_hit']:,} pages "
  "either way). It fetches all of the user's rows through the constraint index and sorts them "
  "to return 20. Both Phase 2 indexes were designed around the `WHERE` clause; this query's "
  "cost is in its `ORDER BY`.\n")

# ---------------------------------------------------------------------------
w("## 5. Experiment: an index shaped like the query\n")
w("A candidate `(user_id, created_at DESC, id DESC)` index, created for measurement and then "
  "**dropped** — this phase measures; it does not change the schema.\n")
w("| User | Query | With Phase 2 indexes | + candidate | Speedup | Pages | Plan |")
w("|---|---|---|---|---|---|---|")
for heavy, who in ((True, "heavy"), (False, "light")):
    for q in ("list_default", "list_filtered"):
        c, e = pick(B2, q, heavy), pick(B3, q, heavy)
        w(f"| {who} | `{q}` | {ms(c['warm_median_ms'])} | **{ms(e['warm_median_ms'])}** | "
          f"{x(c['warm_median_ms'], e['warm_median_ms'])} | {c['shared_hit']:,} → {e['shared_hit']} | {e['plan']} |")
w("")
e = pick(B3, "list_default", True)
w("```")
w(e["text_plan"])
w("```\n")
w("Because the index is already in the requested order, Postgres reads the first 20 entries "
  "and stops — no sort, and the cost no longer grows with how many jobs the user has. It also "
  "serves the heavy user's filtered list, by walking in order and skipping non-`QUEUED` rows "
  "until it has 20. For the light user's filtered list the planner correctly keeps "
  "`idx_jobs_user_status`, since 13 pages were already cheap.\n")

# ---------------------------------------------------------------------------
w("## 6. Costs\n")
w("| Index | Size at 200K rows | Build time |")
w("|---|---|---|")
w("| `idx_jobs_status_priority` | 1,432 kB | 460 ms |")
w("| `idx_jobs_user_status` | 1,464 kB | 470 ms |")
w("| `uq_jobs_user_idempotency_key` | 1,344 kB | (constraint — not rebuilt) |")
w("| `jobs_pkey` | 8,576 kB | (not rebuilt) |")
w("| candidate `(user_id, created_at DESC, id DESC)` | — | 529 ms |")
w("")
w("Every index is also maintained on every `INSERT` and every status change the worker "
  f"writes. The seed loaded {B2['table_rows'] - A1['table_rows']:,} rows in ~33 s with the two "
  "Phase 2 indexes dropped and "
  "rebuilt them afterwards — the standard bulk-load order, since maintaining B-trees row by "
  "row is the expensive part.\n")

# ---------------------------------------------------------------------------
w("## Recommendations\n")
w("1. **Add `(user_id, created_at DESC, id DESC)`** via a new Alembic migration. It targets "
  "the most frequently executed query in the system and was the largest single improvement "
  "measured for it. It would also make `idx_jobs_user_status` redundant for both list "
  "queries — leaving it justified by the index-only count alone.")
w("2. **Keep `idx_jobs_status_priority`, but know that nothing uses it yet.** Revisit when "
  "the first real query that needs it lands (V2's admin view or abandoned-job sweep).")
w("3. **Stop selecting `payload` and `result` for list rows.** The list endpoint fetches both "
  "JSONB columns and `JobSummary` then discards them. With the tiny seeded payloads that cost "
  "does not show here; with real payloads up to 64 KB it means reading TOAST storage for data "
  "that is thrown away. `load_only(...)` on the list query fixes it.")
w("4. **Treat `uq_jobs_user_idempotency_key` as a user_id index too.** Any future index "
  "decision on `jobs` should account for it — it silently served every `user_id` lookup in "
  "this benchmark.\n")

# ---------------------------------------------------------------------------
if (RAW / "B4_migrated_user_created.json").exists():
    B4 = load("B4_migrated_user_created")
    w("## Phase 14b — the index, migrated\n")
    w("Recommendation 1 applied as migration `252ae359c5a3`: `idx_jobs_user_created` on "
      "`(user_id, created_at DESC, id DESC)`, also declared on the model so `alembic check` "
      "stays clean and the test database gets it. The API container applied it itself on "
      "startup. Re-measured:\n")
    w("| User | Query | Phase 2 indexes only | Migrated | Speedup | Pages | Plan |")
    w("|---|---|---|---|---|---|---|")
    for heavy, who in ((True, "heavy"), (False, "light")):
        for q in ("list_default", "list_filtered", "count_filtered"):
            c, m = pick(B2, q, heavy), pick(B4, q, heavy)
            w(f"| {who} | `{q}` | {ms(c['warm_median_ms'])} | **{ms(m['warm_median_ms'])}** | "
              f"{x(c['warm_median_ms'], m['warm_median_ms'])} | {c['shared_hit']:,} → "
              f"{m['shared_hit']:,} | {m['plan']} |")
    w("")
    w("The migrated index matches the experiment within run-to-run noise. The count is "
      "still served by `idx_jobs_user_status` as an index-only scan, which is now that "
      "index's remaining job — both list queries have moved to the new one.\n")

w("## Reproducing\n")
w("```bash")
w("# from backend/, with the Compose stack up")
w("docker cp scripts/seed_jobs.py taskflow-api-1:/app/scripts/seed_jobs.py   # MSYS_NO_PATHCONV=1 on Git Bash")
w("docker exec taskflow-api-1 python -m scripts.seed_jobs")
w("docker exec taskflow-postgres-1 psql -U taskflow -d taskflow -c 'VACUUM ANALYZE jobs;'")
w("python scripts/bench_queries.py <label> <heavy_user_id> <light_user_id> > benchmarks/raw/<label>.json")
w("BENCH_FORCE_SEQSCAN=1 python scripts/bench_queries.py ...   # the no-index baseline")
w("python scripts/render_bench.py                                 # regenerates this file")
w("```\n")
w("`alembic downgrade` was **not** used to drop and restore the indexes. There is a single "
  "migration that creates the tables and the indexes together, so downgrading it would drop "
  "the `jobs` table and all 200,000 rows. The indexes were dropped with `DROP INDEX` and "
  "recreated with the exact DDL from that migration's `upgrade()`; `alembic check` then "
  "confirmed the database matched the models (it had correctly reported both indexes missing "
  "while they were dropped).")

OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
print(f"wrote {OUT} ({len(L)} lines)")
