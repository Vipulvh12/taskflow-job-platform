# Phase 14 — Index Benchmark Results

All query timings are Postgres-internal `Execution Time` from `EXPLAIN (ANALYZE, BUFFERS)`, **median of 6 warm runs** after one discarded cold run. Timings, buffer counts, plans and row counts are generated from `benchmarks/raw/*.json` by `scripts/render_bench.py`, so they cannot drift from the measurements. Index sizes, build times and the heap size were read from `psql` during the run.

## Setup

- **Small table:** 447 rows — real dev data from 13 phases of testing.
- **Large table:** 200,000 rows — the same real rows plus seeded ones spread across 20 dedicated `bench-NN@taskflow.local` users (~5% of the table each). 24 MB heap, 3,078 pages.
- **Heavy user:** 10,214 rows (5.1% of the table). **Light user:** 443 rows (0.2%).
- `VACUUM ANALYZE jobs` before every measurement set, so the planner works from current statistics and the visibility map allows index-only scans.
- Queries are the **exact SQL** `job_service.list_jobs` compiles to, captured from SQLAlchemy — not simplified approximations. That includes all 12 columns and `ORDER BY created_at DESC, id DESC`.

| Query | SQL shape | Where it runs |
|---|---|---|
| `list_default` | `WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 20` | `GET /jobs` — **polled every 2s by the job list** |
| `list_filtered` | same, `AND status='QUEUED'` | `GET /jobs?status=QUEUED` |
| `count_filtered` | `count(*)` over the filtered set | the `total` in every filtered list response |
| `priority_queue` | `WHERE status='QUEUED' ORDER BY priority DESC LIMIT 50` | admin / scheduler shape — **no endpoint issues this today** |

## 1. At 447 rows, indexes don't matter

| Query | With indexes | Without | Plan with indexes |
|---|---|---|---|
| `list_default` | 0.367 ms | 0.377 ms | Limit > Sort > Seq Scan |
| `list_filtered` | 0.371 ms | 0.244 ms | Limit > Sort > Seq Scan |
| `count_filtered` | 0.143 ms | 0.214 ms | Aggregate > Index Only Scan [idx_jobs_user_status] |
| `priority_queue` | 0.069 ms | 0.319 ms | Limit > Index Scan [idx_jobs_status_priority] |

Every query is under half a millisecond either way, and the whole table is 12 pages. For the list queries the planner **ignores** `idx_jobs_user_status` even when it exists: this user owns 443 of the 447 rows, so an index cannot narrow anything and a sequential scan is correctly cheaper. The lesson from this tier is that an index only pays off once the table is large *and* the predicate is selective.

## 2. The finding that changed the experiment

Dropping `idx_jobs_user_status` did **not** produce a sequential scan for the per-user queries. The planner switched to **`uq_jobs_user_idempotency_key`** — the index that backs Phase 5's idempotency constraint. It is on `(user_id, idempotency_key)`, and since `user_id` is its **leading column** it serves `WHERE user_id = ?` perfectly well.

```
Limit  (cost=3600.09..3600.14 rows=20 width=345) (actual time=10.787..10.794 rows=20 loops=1)
  Buffers: shared hit=2980
  ->  Sort  (cost=3600.09..3626.01 rows=10367 width=345) (actual time=10.786..10.789 rows=20 loops=1)
        Sort Key: created_at DESC, id DESC
        Sort Method: top-N heapsort  Memory: 29kB
        Buffers: shared hit=2980
        ->  Bitmap Heap Scan on jobs  (cost=116.64..3324.23 rows=10367 width=345) (actual time=1.612..8.078 rows=10214 loops=1)
              Recheck Cond: (user_id = 'bbe8c878-6912-4be2-8e82-616fbc2bb8b9'::uuid)
              Heap Blocks: exact=2970
              Buffers: shared hit=2980
              ->  Bitmap Index Scan on uq_jobs_user_idempotency_key  (cost=0.00..114.05 rows=10367 width=0) (actual time=0.982..0.983 rows=10214 loops=1)
                    Index Cond: (user_id = 'bbe8c878-6912-4be2-8e82-616fbc2bb8b9'::uuid)
                    Buffers: shared hit=10
Planning Time: 0.166 ms
Execution Time: 10.930 ms
```

So for `user_id` lookups, `idx_jobs_user_status` is partly redundant with an index that already had to exist. Dropping one index does not give a no-index baseline for Query 1; a true worst case needed index access paths disabled for the session (`enable_indexscan`, `enable_bitmapscan`, `enable_indexonlyscan = off`). The large-table results therefore compare **three** conditions:

1. **No index usable** — every index path disabled. The genuine worst case.
2. **Constraint index only** — `idx_jobs_user_status` and `idx_jobs_status_priority` dropped; `uq_jobs_user_idempotency_key` still present, as it always would be.
3. **With the Phase 2 indexes** — the schema as migrated.

## 3. At 200,000 rows

| User | Query | No index | Constraint index only | With Phase 2 indexes | vs no index | vs constraint only |
|---|---|---|---|---|---|---|
| heavy | `list_default` | 24.42 ms | 8.71 ms | **9.05 ms** | 3× | ≈ same |
| heavy | `list_filtered` | 22.58 ms | 7.86 ms | **2.43 ms** | 9× | 3× |
| heavy | `count_filtered` | 25.49 ms | 9.16 ms | **0.481 ms** | 53× | 19× |
| light | `list_default` | 25.33 ms | 0.245 ms | **0.255 ms** | 99× | ≈ same |
| light | `list_filtered` | 22.62 ms | 0.271 ms | **0.258 ms** | 88× | ≈ same |
| light | `count_filtered` | 21.87 ms | 0.184 ms | **0.114 ms** | 192× | 2× |
| all | `priority_queue` | 34.33 ms | 33.60 ms | **0.077 ms** | 446× | 436× |

### Pages touched (8 KB shared buffers)

| User | Query | No index | Constraint only | With indexes | Plan with indexes |
|---|---|---|---|---|---|
| heavy | `list_default` | 3,168 | 2,980 | **2,980** | Limit > Sort > Bitmap Heap Scan > Bitmap Index Scan [uq_jobs_user_idempotency_key] |
| heavy | `list_filtered` | 3,168 | 2,980 | **1,313** | Limit > Sort > Bitmap Heap Scan > Bitmap Index Scan [idx_jobs_user_status] |
| heavy | `count_filtered` | 3,078 | 2,980 | **5** | Aggregate > Index Only Scan [idx_jobs_user_status] |
| light | `list_default` | 3,168 | 14 | **14** | Limit > Sort > Bitmap Heap Scan > Bitmap Index Scan [uq_jobs_user_idempotency_key] |
| light | `list_filtered` | 3,168 | 14 | **13** | Limit > Sort > Bitmap Heap Scan > Bitmap Index Scan [idx_jobs_user_status] |
| light | `count_filtered` | 3,078 | 14 | **4** | Aggregate > Index Only Scan [idx_jobs_user_status] |
| all | `priority_queue` | 3,150 | 3,150 | **35** | Limit > Index Scan [idx_jobs_status_priority] |

Pages are the more honest measure of work than milliseconds: everything here was served from memory (`read=0` throughout), so timings would grow far faster than these ratios suggest once the table no longer fits in cache.

## 4. What each index actually earns

**`idx_jobs_status_priority` — the clearest win.** `priority_queue` goes from 34.33 ms to 0.077 ms (**446×**), and from 3,150 pages to 35. Postgres walks the index backward within `status = 'QUEUED'` and stops after 50 entries — no sort, no table scan:

```
Limit  (cost=0.42..19.57 rows=50 width=18) (actual time=0.015..0.046 rows=50 loops=1)
  Buffers: shared hit=35
  ->  Index Scan Backward using idx_jobs_status_priority on jobs  (cost=0.42..12851.01 rows=33553 width=18) (actual time=0.014..0.039 rows=50 loops=1)
        Index Cond: ((status)::text = 'QUEUED'::text)
        Buffers: shared hit=35
Planning Time: 0.104 ms
Execution Time: 0.063 ms
```

The caveat: **no endpoint issues this query today.** RabbitMQ, not Postgres, orders work for the worker. The index earns its keep only once something — an admin queue view, a scheduler, or V2's abandoned-job sweep — actually asks Postgres for the next-priority queued job. Until then it is paid for on every insert and used by nothing.

**`idx_jobs_user_status` — real, but narrower than it looks.** Its value is the `status` column, since `user_id` alone is already covered. The count becomes an **index-only scan** that never touches the table: 9.16 ms → 0.481 ms and **2,980 → 5 pages** for the heavy user. The filtered list improves 3× over the constraint index alone, but still sorts its matches:

```
Limit  (cost=2822.52..2822.57 rows=20 width=408) (actual time=2.255..2.260 rows=20 loops=1)
  Buffers: shared hit=1313
  ->  Sort  (cost=2822.52..2826.87 rows=1739 width=408) (actual time=2.253..2.256 rows=20 loops=1)
        Sort Key: created_at DESC, id DESC
        Sort Method: top-N heapsort  Memory: 28kB
        Buffers: shared hit=1313
        ->  Bitmap Heap Scan on jobs  (cost=26.24..2776.25 rows=1739 width=408) (actual time=0.478..1.836 rows=1724 loops=1)
              Recheck Cond: ((user_id = 'bbe8c878-6912-4be2-8e82-616fbc2bb8b9'::uuid) AND ((status)::text = 'QUEUED'::text))
              Heap Blocks: exact=1309
              Buffers: shared hit=1313
              ->  Bitmap Index Scan on idx_jobs_user_status  (cost=0.00..25.81 rows=1739 width=0) (actual time=0.236..0.236 rows=1724 loops=1)
                    Index Cond: ((user_id = 'bbe8c878-6912-4be2-8e82-616fbc2bb8b9'::uuid) AND ((status)::text = 'QUEUED'::text))
                    Buffers: shared hit=4
Planning Time: 0.170 ms
Execution Time: 2.299 ms
```

**Neither index helps the hottest query.** `list_default` — the page the job list polls every 2 seconds — runs the same plan with or without the Phase 2 indexes (8.71 ms vs 9.05 ms, 2,980 pages either way). It fetches all of the user's rows through the constraint index and sorts them to return 20. Both Phase 2 indexes were designed around the `WHERE` clause; this query's cost is in its `ORDER BY`.

## 5. Experiment: an index shaped like the query

A candidate `(user_id, created_at DESC, id DESC)` index, created for measurement and then **dropped** — this phase measures; it does not change the schema.

| User | Query | With Phase 2 indexes | + candidate | Speedup | Pages | Plan |
|---|---|---|---|---|---|---|
| heavy | `list_default` | 9.05 ms | **0.050 ms** | 181× | 2,980 → 23 | Limit > Index Scan [idx_experiment_user_created] |
| heavy | `list_filtered` | 2.43 ms | **0.122 ms** | 20× | 1,313 → 119 | Limit > Index Scan [idx_experiment_user_created] |
| light | `list_default` | 0.255 ms | **0.039 ms** | 7× | 14 → 16 | Limit > Index Scan [idx_experiment_user_created] |
| light | `list_filtered` | 0.258 ms | **0.291 ms** | ≈ same | 13 → 13 | Limit > Sort > Bitmap Heap Scan > Bitmap Index Scan [idx_jobs_user_status] |

```
Limit  (cost=0.42..26.43 rows=20 width=456) (actual time=0.099..0.115 rows=20 loops=1)
  Buffers: shared hit=23
  ->  Index Scan using idx_experiment_user_created on jobs  (cost=0.42..12747.23 rows=9800 width=456) (actual time=0.097..0.111 rows=20 loops=1)
        Index Cond: (user_id = 'bbe8c878-6912-4be2-8e82-616fbc2bb8b9'::uuid)
        Buffers: shared hit=23
Planning Time: 0.134 ms
Execution Time: 0.151 ms
```

Because the index is already in the requested order, Postgres reads the first 20 entries and stops — no sort, and the cost no longer grows with how many jobs the user has. It also serves the heavy user's filtered list, by walking in order and skipping non-`QUEUED` rows until it has 20. For the light user's filtered list the planner correctly keeps `idx_jobs_user_status`, since 13 pages were already cheap.

## 6. Costs

| Index | Size at 200K rows | Build time |
|---|---|---|
| `idx_jobs_status_priority` | 1,432 kB | 460 ms |
| `idx_jobs_user_status` | 1,464 kB | 470 ms |
| `uq_jobs_user_idempotency_key` | 1,344 kB | (constraint — not rebuilt) |
| `jobs_pkey` | 8,576 kB | (not rebuilt) |
| candidate `(user_id, created_at DESC, id DESC)` | — | 529 ms |

Every index is also maintained on every `INSERT` and every status change the worker writes. The seed loaded 199,553 rows in ~33 s with the two Phase 2 indexes dropped and rebuilt them afterwards — the standard bulk-load order, since maintaining B-trees row by row is the expensive part.

## Recommendations

1. **Add `(user_id, created_at DESC, id DESC)`** via a new Alembic migration. It targets the most frequently executed query in the system and was the largest single improvement measured for it. It would also make `idx_jobs_user_status` redundant for both list queries — leaving it justified by the index-only count alone.
2. **Keep `idx_jobs_status_priority`, but know that nothing uses it yet.** Revisit when the first real query that needs it lands (V2's admin view or abandoned-job sweep).
3. **Stop selecting `payload` and `result` for list rows.** The list endpoint fetches both JSONB columns and `JobSummary` then discards them. With the tiny seeded payloads that cost does not show here; with real payloads up to 64 KB it means reading TOAST storage for data that is thrown away. `load_only(...)` on the list query fixes it.
4. **Treat `uq_jobs_user_idempotency_key` as a user_id index too.** Any future index decision on `jobs` should account for it — it silently served every `user_id` lookup in this benchmark.

## Reproducing

```bash
# from backend/, with the Compose stack up
docker cp scripts/seed_jobs.py taskflow-api-1:/app/scripts/seed_jobs.py   # MSYS_NO_PATHCONV=1 on Git Bash
docker exec taskflow-api-1 python -m scripts.seed_jobs
docker exec taskflow-postgres-1 psql -U taskflow -d taskflow -c 'VACUUM ANALYZE jobs;'
python scripts/bench_queries.py <label> <heavy_user_id> <light_user_id> > benchmarks/raw/<label>.json
BENCH_FORCE_SEQSCAN=1 python scripts/bench_queries.py ...   # the no-index baseline
python scripts/render_bench.py                                 # regenerates this file
```

`alembic downgrade` was **not** used to drop and restore the indexes. There is a single migration that creates the tables and the indexes together, so downgrading it would drop the `jobs` table and all 200,000 rows. The indexes were dropped with `DROP INDEX` and recreated with the exact DDL from that migration's `upgrade()`; `alembic check` then confirmed the database matched the models (it had correctly reported both indexes missing while they were dropped).
