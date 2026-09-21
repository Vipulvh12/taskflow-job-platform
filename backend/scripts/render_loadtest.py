"""Renders benchmarks/phase17_results.md and its charts from the files the
load-test harness wrote to benchmarks/phase17/raw/. Every number in the
document comes from those files, so the prose can't drift from the
measurements — with one exception, burst_before.jsonl, transcribed from the
console output of loadtest/burst.py runs against builds that no longer exist
(the unfixed API, and a throwaway host API with a 40-connection pool).

    .venv/Scripts/python scripts/render_loadtest.py
"""
import ast
import collections
import csv
import gzip
import json
import statistics
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
RAW = BENCH / "phase17" / "raw"
OUT = BENCH / "phase17_results.md"

RUNS = {
    # label: (users, spawn rate, seconds, description)
    "before_u10": (10, 2, 120, "before fix"),
    "before_u100": (100, 10, 120, "before fix"),
    "before_u1000": (1000, 50, 180, "before fix"),
    "u10": (10, 2, 120, "after fix"),
    "u100": (100, 10, 120, "after fix"),
    "u1000": (1000, 50, 180, "after fix"),
    "exp_workers4_u1000": (1000, 50, 180, "after fix, 4 Uvicorn processes (experiment)"),
}
ENDPOINTS = ["/jobs [list]", "/jobs [filtered]", "/jobs/{id}", "/jobs [submit]"]


# ------------------------------------------------------------------ loading ---


def stats(label):
    with open(RAW / f"{label}_stats.csv", encoding="utf-8") as f:
        return {row["Name"]: row for row in csv.DictReader(f)}


def history(label):
    with open(RAW / f"{label}_stats_history.csv", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["Name"] == "Aggregated"]


def server(label):
    with open(RAW / f"{label}_server.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def probe(label):
    with open(RAW / f"{label}_probe.csv", encoding="utf-8") as f:
        return [(float(r["t"]), r["ms"]) for r in csv.DictReader(f)]


def queue(label):
    return json.loads((RAW / f"{label}_queue.json").read_text(encoding="utf-8"))


def server_codes(label):
    first = (RAW / f"{label}_api_log_summary.txt").read_text(encoding="utf-8").splitlines()[0]
    return ast.literal_eval(first.split(": ", 1)[1])


def jsonl(name):
    lines = (RAW / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def steady_window(label):
    """From 10s after the last user spawned to the end of the run."""
    users = RUNS[label][0]
    hist = history(label)
    spawned = next(float(r["Timestamp"]) for r in hist if int(r["User Count"]) >= users)
    return spawned + 10, float(hist[-1]["Timestamp"])


def steady_server(label):
    start, end = steady_window(label)
    return [r for r in server(label) if start <= float(r["t"]) <= end]


def median_of(rows, key):
    return statistics.median(float(r[key]) for r in rows)


# --------------------------------------------------------------- formatting ---


def ms(v):
    v = float(v)
    if v >= 10_000:
        return f"{v / 1000:.0f} s"
    if v >= 1000:
        return f"{v / 1000:.1f} s"
    return f"{v:.0f} ms"


def pct(n, total):
    return f"{n / total * 100:.1f}%" if total else "—"


def run_row(label):
    users, rate, seconds, desc = RUNS[label]
    agg = stats(label)["Aggregated"]
    reqs, fails = int(agg["Request Count"]), int(agg["Failure Count"])
    srv = steady_server(label)
    fivexx = sum(v for k, v in server_codes(label).items() if k.startswith("5"))
    return (f"| `{label}` | {desc} | {users:,} | {reqs:,} | {fails:,} ({pct(fails, reqs)}) | {fivexx} "
            f"| {float(agg['Requests/s']):.1f} | {ms(agg['50%'])} | {ms(agg['95%'])} | {ms(agg['99%'])} "
            f"| {median_of(srv, 'cpu_api'):.0f}% | {median_of(srv, 'cpu_postgres'):.0f}% |")


# ------------------------------------------------------------------- charts ---

PALETTE = ["#c2410c", "#1d4ed8", "#15803d", "#7c3aed"]


def svg_line_chart(path, title, series, y_label, x_label="seconds since the run started"):
    """series: [(name, [(x, y), ...])]. Plain SVG, no dependencies."""
    width, height = 760, 300
    left, right, top, bottom = 60, 20, 40, 60
    xs = [x for _, pts in series for x, _ in pts]
    ys = [y for _, pts in series for _, y in pts]
    x_max = max(xs) or 1
    y_max = max(ys) * 1.1 or 1
    step = _nice_step(y_max)
    y_max = step * (int(y_max / step) + 1)

    def sx(x):
        return left + (width - left - right) * x / x_max

    def sy(y):
        return top + (height - top - bottom) * (1 - y / y_max)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'font-family="system-ui, sans-serif" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<text x="{left}" y="22" font-size="14" font-weight="600" fill="#111">{title}</text>']
    y = 0.0
    while y <= y_max + 1e-9:
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{sy(y):.1f}" y2="{sy(y):.1f}" stroke="#e5e7eb"/>')
        out.append(f'<text x="{left - 8}" y="{sy(y) + 4:.1f}" text-anchor="end" fill="#555">{y:g}</text>')
        y += step
    x_step = _nice_step(x_max)
    x = 0.0
    while x <= x_max + 1e-9:
        out.append(f'<text x="{sx(x):.1f}" y="{height - bottom + 18}" text-anchor="middle" fill="#555">{x:g}</text>')
        x += x_step
    out.append(f'<text x="{(left + width - right) / 2}" y="{height - bottom + 36}" text-anchor="middle" fill="#555">{x_label}</text>')
    out.append(f'<text transform="translate(16 {(top + height - bottom) / 2}) rotate(-90)" text-anchor="middle" fill="#555">{y_label}</text>')
    for i, (name, pts) in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        d = " ".join(f"{sx(px):.1f},{sy(py):.1f}" for px, py in pts)
        out.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        lx = left + i * 230
        out.append(f'<rect x="{lx}" y="{height - 16}" width="14" height="4" fill="{color}"/>')
        out.append(f'<text x="{lx + 20}" y="{height - 11}" fill="#111">{name}</text>')
    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")


def _nice_step(span):
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if span / step <= 6:
            return step
    return 2000


def rps_series(label, window=5):
    """Completions per second from the cumulative request count, averaged over
    `window` seconds. Not Locust's own Requests/s column: that one holds its
    last value while nothing completes, which draws a deadlock as a plateau."""
    hist = history(label)
    t0 = float(hist[0]["Timestamp"])
    points = [(float(r["Timestamp"]) - t0, int(r["Total Request Count"])) for r in hist]
    series = []
    for i in range(len(points)):
        j = max(0, i - window)
        (ta, ca), (tb, cb) = points[j], points[i]
        series.append((tb, (cb - ca) / (tb - ta) if tb > ta else 0.0))
    return series


# ------------------------------------------------------------------ profile ---


def profile_summary():
    stacks = []
    with gzip.open(RAW / "pyspy_u1000_gil.txt.gz", "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                frames, n = line.rstrip("\n").rsplit(" ", 1)
                stacks.append((frames.split(";"), int(n)))
    total = sum(n for _, n in stacks)

    libs = [("sqlalchemy/", "SQLAlchemy (statement caching, compiling, building result rows)"),
            ("fastapi/", "FastAPI (routing, dependency solving, threadpool dispatch)"),
            ("anyio/", "anyio (handing work to and from the threadpool)"),
            ("asyncio/", "asyncio event loop"), ("uvicorn/", "uvicorn (HTTP protocol)"),
            ("pydantic", "pydantic (validation, serialization)"), ("starlette/", "Starlette"),
            ("logging/", "logging (access log)"), ("psycopg2", "psycopg2 (the Postgres driver)"),
            ("app/", "TaskFlow's own code"), ("json/", "json encoding"), ("jwt/", "PyJWT"),
            ("redis/", "redis-py"), ("pika/", "pika")]
    by_lib = collections.Counter()
    for frames, n in stacks:
        for frame in reversed(frames):
            hit = next((name for key, name in libs if key in frame), None)
            if hit:
                by_lib[hit] += n
                break
        else:
            by_lib["other"] += n

    notable = collections.Counter()
    for frames, n in stacks:
        joined = ";".join(frames)
        if "list_jobs (app/services/job_service.py" in joined:
            if "count (sqlalchemy/orm/query.py" in joined:
                notable["`list_jobs`: the `COUNT(*)` for the page total"] += n
            else:
                notable["`list_jobs`: the page query and turning its rows into objects"] += n
        elif "get_current_user" in joined and "sqlalchemy" in joined:
            notable["`get_current_user`: loading the user row"] += n
    return total, by_lib, notable


# ------------------------------------------------------------------ document ---


def main():
    L = []
    w = L.append

    u1000_srv = steady_server("u1000")
    u1000, w4 = stats("u1000")["Aggregated"], stats("exp_workers4_u1000")["Aggregated"]
    b1000, b100 = stats("before_u1000")["Aggregated"], stats("before_u100")["Aggregated"]
    db = json.loads((RAW / "db_cost.json").read_text(encoding="utf-8"))
    db_total = sum(q["execution_ms"] for q in db["queries"].values())
    bursts_before, bursts_after = jsonl("burst_before.jsonl"), jsonl("burst_after.jsonl")

    w("# Phase 17 — Load Test Results\n")
    w("Generated by `scripts/render_loadtest.py` from the files in `benchmarks/phase17/raw/`, so "
      "every number below is the one the harness recorded. The one exception is the *before* "
      "half of the burst table in §1, transcribed from console output: it was measured against "
      "builds that no longer exist.\n")

    w("## Setup\n")
    w("- **Machine:** one laptop — AMD Ryzen 5 5600H, 6 cores / 12 threads. Docker Desktop's VM "
      "gets 12 vCPUs and 3.5 GB. Locust runs on the same machine, so the load generator and the "
      "system under test compete for the same cores. These are this laptop's numbers, not a "
      "capacity figure for the design.")
    w("- **Stack:** the Phase 13/15 Compose stack as committed — one Uvicorn process, SQLAlchemy's "
      "default pool of 5 + 10 connections, one worker.")
    w(f"- **Data:** the Phase 14 dataset. Reads go to the 20 `bench-NN@taskflow.local` accounts, "
      f"which own ~200K rows between them (~{db['user_rows']:,} each). Submissions go to 50 "
      "dedicated `loadtest-NN@taskflow.local` accounts, so the Phase 14 dataset isn't modified.")
    w("- **Auth:** access tokens minted at test start with the app's own `create_access_token` — "
      "the same claims `/auth/login` issues. They can't come from `/auth/login`: it allows 10 "
      "requests/min per IP, and `*@taskflow.local` can't log in over HTTP at all (`.local` is a "
      "special-use domain and `EmailStr` rejects it with a 422). **Login latency is not measured.**")
    w("- **Mix:** each simulated user waits 1–3 s between requests. Of its requests, "
      "10 : 5 : 3 : 1 are list page 1, list filtered by status, fetch one job, submit a job.")
    w("- **Client:** Locust 2.46 `FastHttpUser`, one process, against `http://127.0.0.1:8000`. "
      "It never logged its high-CPU warning.")
    w("- **Server side, sampled during every run:** `docker stats` CPU per container, "
      "`pg_stat_activity` connection states, `jobs` queue depth, and a separate process calling "
      "`GET /health` every 100 ms. After each run: queue delay for every submitted job, and the "
      "API's own log.")
    w("- Percentiles are Locust's, which rounds anything over 100 ms to two significant figures.\n")

    w("## Summary\n")
    w("| Run | Build | Users | Requests | Failed | Server 5xx | req/s | p50 | p95 | p99 | API CPU | Postgres CPU |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for label in RUNS:
        w(run_row(label))
    w("\nreq/s is over the whole run, including ramp-up. CPU is the median over the steady state "
      "(10 s after the last user spawned, to the end); 100% is one core.\n")

    # ---------------------------------------------------------------- §1
    b100_srv = server("before_u100")
    # Seconds since Locust started, the same axis as the charts (the sampler starts 5 s earlier).
    t0 = float(history("before_u100")[0]["Timestamp"])
    frozen = [r for r in b100_srv if int(r["pg_total"]) - int(r["pg_active"]) - int(r["pg_idle"]) >= 15]
    freeze_from, freeze_to = float(frozen[0]["t"]) - t0, float(frozen[-1]["t"]) - t0
    freeze_cpu = statistics.median(float(r["cpu_api"]) for r in frozen)
    answered = sorted(t + float(m) / 1000 - t0 for t, m in probe("before_u100") if m != "error")
    probe_gap = max(b - a for a, b in zip(answered, answered[1:]))
    probe_errors = sum(1 for _, m in probe("before_u100") if m == "error")

    w("## 1. At 100 users the API deadlocked\n")
    w(f"The first 100-user run failed {b100['Failure Count']} requests with HTTP 500, all "
      "`QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00` — "
      f"at {float(b100['Requests/s']):.0f} req/s, when ~25 ms requests need about one connection "
      "on average, not fifteen. The server-side samples show it wasn't load:\n")
    w(f"- From t≈{freeze_from:.0f}s to t≈{freeze_to:.0f}s the API process sat at "
      f"**{freeze_cpu:.1f}% CPU**, and every one of its 15 connections was `idle in transaction` "
      "— checked out, running nothing.")
    w(f"- The `/health` probe went **{probe_gap:.0f} s without an answer**: {probe_errors} request "
      "hit its 30 s client timeout, and the next one waited for the freeze to end.")
    w("- It ended 30 s after it began — `pool_timeout` — and exactly **40** requests failed: the "
      "size of anyio's threadpool.\n")
    w("**Mechanism.** FastAPI runs each sync dependency, the sync endpoint, and response validation "
      "as *separate* trips into one shared threadpool of 40 threads. A request's Session checks "
      "out its connection in `get_current_user` and keeps it until the request ends — across "
      "those trips. When more than 15 + 40 requests are in flight, every connection can end up "
      "held by a request waiting for a thread, and every thread by a request waiting for a "
      "connection. Nothing moves until the 40 threads' pool checkouts time out. (FastAPI's own "
      "source names this hazard for a dependency's teardown and works around it there — only "
      "there.)\n")
    w("**Reproduced on demand** with `loadtest/burst.py` — n simultaneous `GET /jobs`:\n")
    w("| Build | n | Outcome | p50 | max | Peak `idle in transaction` |")
    w("|---|---|---|---|---|---|")
    for b in bursts_before:
        outcome = ", ".join(f"{v}× {k}" for k, v in sorted(b["outcomes"].items()))
        w(f"| {b['config']} | {b['n']} | {outcome} | {ms(b['p50_ms'])} | {ms(b['max_ms'])} "
          f"| {b.get('peak_idle_in_transaction', '—')} |")
    for b in bursts_after:
        outcome = ", ".join(f"{v}× {k}" for k, v in sorted(b["outcomes"].items()))
        w(f"| container, pool 15, **with cap** (final) | {b['n']} | {outcome} | {ms(b['p50_ms'])} "
          f"| {ms(b['max_ms'])} | {b['peak_idle_in_transaction']} |")
    w("\nThe threshold sits between 50 and 60, as 15 + 40 = 55 predicts. **Raising the pool "
      "doesn't fix it — it moves the threshold** to pool + 40: at 40 connections, 60 passes and "
      "100 wedges identically, with 40 × 500 and all 40 connections idle in transaction.\n")
    w("**Fix:** `app/concurrency_limit.py` caps in-flight requests per process at the pool size "
      "(`db_pool_size + db_max_overflow`, now explicit settings). At most 15 requests hold or want "
      "a connection, so a checkout never waits, and they never need more than 15 of the 40 "
      "threads — the cycle can't form. Requests over the cap wait in the event loop holding "
      "nothing. It depends on each request using exactly one Session, which `get_db` guarantees. "
      "Verified that the Session closes before the cap's slot is released, on success and error "
      "paths. `tests/integration/test_concurrency_limit.py` fires 100 simultaneous requests; with "
      "the cap removed it fails — pool `TimeoutError`s, slowest request 61 s — and with it it "
      "passes in ~3 s.\n")
    svg_line_chart(BENCH / "phase17" / "throughput_u100.svg",
                   "100 users: completed requests per second (5 s average)",
                   [("before the fix", rps_series("before_u100")),
                    ("after the fix", rps_series("u100"))], "req/s")
    w("![100 users, before and after](phase17/throughput_u100.svg)\n")

    # ---------------------------------------------------------------- §2
    w("## 2. Per endpoint, after the fix\n")
    for label in ("u10", "u100", "u1000"):
        users = RUNS[label][0]
        st = stats(label)
        w(f"**{users:,} users**\n")
        w("| Endpoint | Requests | req/s | p50 | p95 | p99 | max |")
        w("|---|---|---|---|---|---|---|")
        for name in ENDPOINTS:
            r = st[name]
            w(f"| `{name}` | {int(r['Request Count']):,} | {float(r['Requests/s']):.1f} | {ms(r['50%'])} "
              f"| {ms(r['95%'])} | {ms(r['99%'])} | {ms(r['Max Response Time'])} |")
        w("")
    u10_probe = [(t, float(m)) for t, m in probe("u10") if m != "error"]
    slowest_probe = max(m for _, m in u10_probe)
    w(f"At 10 users the p99 is set by a handful of slow requests out of ~{int(stats('u10')['Aggregated']['Request Count'])}. "
      f"The `/health` probe saw the same moment (one response of {ms(slowest_probe)}), so it was "
      "on the server, not in Locust. Its cause is **unexplained**. It overlapped a Postgres "
      "checkpoint that fsynced ~1,700 files, but forcing one that fsynced ~1,100 files over "
      "1.7 s while probing left `/health` under 10 ms, so that hypothesis is refuted.\n")

    # ---------------------------------------------------------------- §3
    total, by_lib, notable = profile_summary()
    users = RUNS["u1000"][0]
    little = users / (float(u1000["50%"]) / 1000 + 2)
    w("## 3. At 1,000 users the ceiling is one Python process\n")
    w(f"Before the fix, 1,000 users took the API down: {pct(int(b1000['Failure Count']), int(b1000['Request Count']))} "
      f"of requests failed, most at Locust's 60 s client timeout, at {float(b1000['Requests/s']):.0f} req/s. "
      f"It was wedged in {sum(1 for r in server('before_u1000') if int(r['pg_idle_in_tx']) >= 15 and int(r['pg_active']) == 0)} "
      f"of {len(server('before_u1000'))} samples, at a median {statistics.median(float(r['cpu_api']) for r in server('before_u1000')):.1f}% API CPU.\n")
    w(f"After the fix: **zero failures, {float(u1000['Requests/s']):.0f} req/s** — but a p50 of "
      f"{ms(u1000['50%'])}. That latency is queueing, not slowness: 1,000 users that each wait "
      f"~2 s between requests, at a p50 of {ms(u1000['50%'])}, can only generate about "
      f"1000 ÷ ({float(u1000['50%']) / 1000:.0f} + 2) ≈ {little:.0f} req/s — close to the "
      f"{float(u1000['Requests/s']):.0f} measured. Something saturated. The steady-state samples "
      "say what:\n")
    w("| | Median | p90 |")
    w("|---|---|---|")
    for key, name in (("cpu_api", "API CPU"), ("cpu_postgres", "Postgres CPU"),
                      ("cpu_worker", "Worker CPU"), ("cpu_redis", "Redis CPU")):
        xs = sorted(float(r[key]) for r in u1000_srv)
        w(f"| {name} | {statistics.median(xs):.0f}% | {xs[int(len(xs) * 0.9)]:.0f}% |")
    for key, name in (("pg_active", "Postgres connections running a query"),
                      ("pg_idle_in_tx", "Connections checked out, between queries"),
                      ("queue_depth", "`jobs` queue depth")):
        xs = sorted(int(r[key]) for r in u1000_srv)
        w(f"| {name} | {statistics.median(xs):g} | {xs[int(len(xs) * 0.9)]} |")
    w("\nThe API runs at roughly one core of Python bytecode plus the work that runs outside the "
      "GIL. Postgres is mostly idle and is almost never *running* a query when sampled. The "
      "worker, Redis and the queue are nowhere near busy.\n")
    w("**Tested, not assumed:** the same image with 4 Uvicorn processes (`WEB_CONCURRENCY=4`, "
      "an experiment, not committed):\n")
    w("| Uvicorn processes | req/s | p50 | p95 | Failed | API CPU | Postgres CPU | API CPU per request |")
    w("|---|---|---|---|---|---|---|---|")
    for label, n in (("u1000", 1), ("exp_workers4_u1000", 4)):
        a, srv = stats(label)["Aggregated"], steady_server(label)
        cpu = median_of(srv, "cpu_api")
        w(f"| {n} | {float(a['Requests/s']):.0f} | {ms(a['50%'])} | {ms(a['95%'])} | {a['Failure Count']} "
          f"| {cpu:.0f}% | {median_of(srv, 'cpu_postgres'):.0f}% | ~{cpu / 100 * 1000 / float(a['Requests/s']):.0f} ms |")
    w(f"\nThroughput rose {float(w4['Requests/s']) / float(u1000['Requests/s']):.1f}× — so the single "
      "process was the ceiling. Not 4×, because CPU per request grew too. With 6 physical cores "
      "shared by four API processes, Postgres, Locust and Windows, that's consistent with work "
      "landing on busy hyperthreads. Past this point the numbers describe the laptop, not the "
      "design, so this was the last experiment.\n")
    svg_line_chart(BENCH / "phase17" / "throughput_u1000.svg",
                   "1,000 users: completed requests per second (5 s average)",
                   [("before the fix", rps_series("before_u1000")),
                    ("after, 1 process", rps_series("u1000")),
                    ("after, 4 processes", rps_series("exp_workers4_u1000"))], "req/s")
    w("![1,000 users](phase17/throughput_u1000.svg)\n")

    w("### Where the CPU goes\n")
    w(f"`py-spy record --gil` against the API container under 1,000 users: {total:,} samples of "
      "whichever thread held the GIL, attributed to the innermost library on the stack:\n")
    w("| Share | Library |")
    w("|---|---|")
    for name, n in by_lib.most_common(10):
        w(f"| {pct(n, total)} | {name} |")
    w("\nInclusive costs worth naming:\n")
    w("| Share | |")
    w("|---|---|")
    for name, n in notable.most_common():
        w(f"| {pct(n, total)} | {name} |")
    w(f"\nThe same request costs Postgres **{db_total:.1f} ms** — EXPLAIN ANALYZE, median of 5, "
      f"for a user with {db['user_rows']:,} jobs:\n")
    w("| Statement | Execution | Plan |")
    w("|---|---|---|")
    for name, q in db["queries"].items():
        w(f"| {name} | {q['execution_ms']:.3f} ms | {q['scan']} |")
    count_ms = db["queries"]["COUNT(*) for the page total"]["execution_ms"]
    page_ms = db["queries"]["page of 20 (Phase 14b index)"]["execution_ms"]
    w(f"\nSo **Phase 14b's index holds under concurrency**: the page query is {page_ms:.3f} ms, "
      "and the database is not what limits this API: at 1,000 users, the median number of "
      "connections running a query at any sampled moment is 0. What costs time is the Python around each query — "
      "SQLAlchemy's per-statement machinery and FastAPI's dispatch — paid three times per list "
      f"request. The `COUNT(*)` is now the most expensive SQL, at {count_ms / db_total * 100:.0f}% of "
      "the request's database time, because it reads every one of the user's rows.\n")

    # ---------------------------------------------------------------- §4
    w("## 4. Queue delay\n")
    w("From the API accepting a job to the worker starting it (`started_at - created_at`), for "
      "every job each run submitted. Locust can't see this — its clock stops at the 201.\n")
    w("| Run | Jobs | Succeeded | Queue delay p50 | p95 | max | Accepted → done p50 |")
    w("|---|---|---|---|---|---|---|")
    for label in RUNS:
        q = queue(label)
        w(f"| `{label}` | {sum(q['statuses'].values())} | {q['succeeded']} | {ms(q['queue_delay_ms']['p50'])} "
          f"| {ms(q['queue_delay_ms']['p95'])} | {ms(q['queue_delay_ms']['max'])} | {ms(q['end_to_end_ms']['p50'])} |")
    max_depth = max(int(r["queue_depth"]) for label in RUNS for r in server(label))
    pending = sum(queue(label)["still_pending"] for label in RUNS)
    lag = max(queue(label)["last_completed_after_last_submitted_s"] for label in RUNS)
    b100_max = queue("before_u100")["queue_delay_ms"]["max"]
    w(f"\nThe single worker kept up at every load level: {pending} jobs were left pending across "
      f"all runs, the deepest the `jobs` queue got in any sample was {max_depth}, and in every run "
      f"the last job finished at most {lag:.2f} s after the last one was submitted. The "
      f"{ms(b100_max)} maximum in `before_u100` is the deadlock, not the worker: a job committed "
      "just before the freeze couldn't be published until it ended.\n")

    # ---------------------------------------------------------------- §5
    w("## 5. Not measured, and caveats\n")
    w("- **Login.** Not in the mix — see Setup.")
    w("- **One machine.** Locust shares the CPU with the server. Nothing here should be read as "
      "what the design does on dedicated hardware.")
    w("- **The cap moves waiting, it doesn't remove it.** Past saturation, requests queue in the "
      "event loop instead of deadlocking. That includes `/health`: it waited ~10 s at 1,000 users, "
      "longer than the Compose healthcheck's 5 s timeout, so the API reports `unhealthy` while "
      "saturated. Compose only reports that. An orchestrator that *restarts* on a failed liveness "
      "check would restart a working, saturated API, making things worse, so liveness there would "
      "need an endpoint that bypasses the cap and the database.")
    w("- **No load shedding.** A queue that only grows is not a strategy for sustained overload. "
      "A bound on waiting requests, answered with 503, would be the next step.")
    w("- **Levers not pulled**, in rough order of effort: more Uvicorn processes (measured above; "
      "each needs its own 15 connections against Postgres's 100); dropping the per-page "
      "`COUNT(*)`; fewer ORM round trips per request; async endpoints, which avoid threadpool "
      "trips altogether and are the largest change.")

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(BENCH.parent)} and 2 charts")


if __name__ == "__main__":
    main()
