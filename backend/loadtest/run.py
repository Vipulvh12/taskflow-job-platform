"""One recorded load-test run: Locust + the server sampler + queue delay + the
API's own log, all written under benchmarks/phase17/raw/. From backend/:

    .venv/Scripts/python -m loadtest.run <users> <spawn_rate> <seconds> <label>

e.g. `... loadtest.run 1000 50 180 u1000`. Nothing here changes server state
beyond the jobs the test itself submits.
"""
import collections
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
RAW = BACKEND / "benchmarks" / "phase17" / "raw"
PY = sys.executable
HOST = "http://127.0.0.1:8000"
BASELINE_SECONDS = 5   # sampler runs alone first, to show the idle state
TRAILING_SECONDS = 10  # and after, to show the recovery

ACCESS_LINE = re.compile(r'"(?:GET|POST) (\S+) [^"]*" (\d{3})')


def main():
    users, rate, seconds, label = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    RAW.mkdir(parents=True, exist_ok=True)
    out = RAW / label

    mark = subprocess.run([PY, "-m", "loadtest.queue_delay", "mark"],
                          cwd=BACKEND, capture_output=True, text=True, check=True).stdout.strip()
    log_since = datetime.now(timezone.utc).isoformat()

    sampler = subprocess.Popen(
        [PY, "-m", "loadtest.sample_server",
         str(BASELINE_SECONDS + seconds + TRAILING_SECONDS), str(out)],
        cwd=BACKEND,
    )
    time.sleep(BASELINE_SECONDS)
    print(f"[{label}] locust: {users} users, spawn {rate}/s, {seconds}s")
    subprocess.run(
        [PY, "-m", "locust", "-f", "loadtest/locustfile.py", "--host", HOST, "--headless",
         "-u", users, "-r", rate, "-t", f"{seconds}s",
         "--csv", str(out), "--html", f"{out}_report.html",
         "--logfile", f"{out}_locust.log", "--only-summary"],
        cwd=BACKEND,
    )
    sampler.wait()

    print(f"[{label}] waiting for the worker to drain submitted jobs")
    subprocess.run([PY, "-m", "loadtest.queue_delay", "report", mark, f"{out}_queue.json"],
                   cwd=BACKEND, check=True)

    # The server's own view of status codes, to cross-check Locust's failure
    # counts, plus every non-access-log line (tracebacks, pool errors).
    logs = subprocess.run(["docker", "logs", "taskflow-api-1", "--since", log_since],
                          capture_output=True, text=True)
    lines = (logs.stdout + logs.stderr).splitlines()
    # /health is the sampler's own probe, not load-test traffic.
    codes = collections.Counter(
        m.group(2) for line in lines
        if (m := ACCESS_LINE.search(line)) and m.group(1) != "/health"
    )
    other = [line for line in lines if not ACCESS_LINE.search(line)]
    with open(f"{out}_api_log_summary.txt", "w") as f:
        f.write(f"status codes (server side): {dict(sorted(codes.items()))}\n")
        f.write(f"non-access-log lines: {len(other)}\n\n")
        f.write("\n".join(other[:200]))
    print(f"[{label}] server-side status codes: {dict(sorted(codes.items()))}; "
          f"other log lines: {len(other)}")


if __name__ == "__main__":
    main()
