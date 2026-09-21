# Design note: a per-job execution timeout

Status: **not implemented** (proposed for V3). Nothing below exists in the code yet.

## Problem

A handler that never returns is never detected, and the job never resolves.

How the worker runs a job today ([worker/main.py](../backend/worker/main.py)):

- The handler runs **synchronously on the worker's main thread**, inside pika's
  `start_consuming()` callback (`on_message` → `process_job` → `_run_claimed` →
  `handler(payload, context)`), with `prefetch_count=1`.
- A **heartbeat thread** renews `job_heartbeat:{job_id}` every 5 s for as long as
  the handler runs. It proves the *process* is alive, not that the job is making
  progress. So the reaper, which only acts on a missing heartbeat, never touches
  a hung job.
- **A database transaction is open for the whole handler call.** `_run_claimed`'s
  `db.get(Job, ...)` begins one after the claim commits, so a hung handler holds
  a Postgres connection `idle in transaction` indefinitely.
- Expected but **not verified in this repo**: pika's `BlockingConnection` only
  services AMQP heartbeats when its I/O loop runs, which it doesn't while the
  handler holds the main thread. A long hang should therefore get the connection
  closed by the broker. The unacked message would then go to another worker,
  whose claim fails (the row is `RUNNING`), so it acks and drops it. The job
  stays `RUNNING` with a live heartbeat and no message anywhere.

The core constraint: **Python cannot safely kill a thread from another thread.**
There is no `Thread.kill()`. Asynchronous exceptions (`PyThreadState_SetAsyncExc`)
land at arbitrary bytecodes and never interrupt C code. So the timeout has to
come from the thread itself (A), from outside the process (B), or by ending the
whole process (C1).

## A. Signal/alarm-based timeout

`signal.setitimer(ITIMER_REAL, t)` before `handler(...)`, a `SIGALRM` handler that
raises `JobTimeout`, and the timer cleared in a `finally` right after the call.
Possible here because handlers run on the main thread, the only thread Python
delivers signals to.

- **Advantages:** about 15 lines, no new processes. The exception flows through
  the existing `_fail()` path, so a timeout becomes a transient failure with the
  normal retry ladder and ends `DEAD` if it keeps happening.
- **Disadvantages:** Python-level only. The signal handler runs between
  bytecodes, so a long-running C call (a C extension loop, a C regex) isn't
  interrupted until it returns. Unix-only (`SIGALRM`), which is fine for the Linux
  containers but not for running the worker on Windows. And the main thread stays
  blocked for up to the timeout, so the AMQP heartbeat concern above remains
  unless the timeout is well under the broker's heartbeat timeout.
- **Effect on database transactions:** handlers never touch the database, so an
  exception raised *inside* the handler is safe: the open read transaction is
  reused by `_fail()` to record the attempt. The risk is the race at the edge.
  If the timer fires after the handler returns but before it's cleared, the
  exception lands in worker code, possibly inside `_record_attempt` or
  `db.commit()`. The session then has to be rolled back and the job's state
  re-read before anything else is written.
- **Cleanup concerns:** the exception can interrupt the handler anywhere,
  including inside `finally` blocks and `__exit__` methods. A partly written PDF
  is harmless (retries overwrite `{job_id}.pdf`), but anything the handler opened
  without a `with` block leaks, and a lock held at that moment (logging's, say)
  may never be released.

## B. A separate process runs the handler

The worker process keeps everything it has now: the database session, the AMQP
channel and the heartbeat. A child process only runs `handler(payload, context)`
and returns the result over a pipe. On timeout, the parent kills the child.

- **Advantages:** a hard guarantee, since SIGKILL works even inside C code.
  Isolation: a handler that leaks memory, or segfaults in a C extension, takes
  down only the child, and the parent records a failure. The parent's transaction
  and channel are never interrupted mid-operation, and while waiting it can keep
  servicing the AMQP connection, which also settles the heartbeat concern above.
- **Disadvantages:** payloads and results must be picklable. They are JSON-shaped
  dicts, so they already are. `HandlerError` must cross the pipe as data, to keep
  the permanent/transient split. A child crash, a timeout and a normal failure
  are three outcomes to map onto the existing taxonomy.
- **Process startup overhead:** measured in the worker container, median of 7
  runs each:

  | Start method | Empty child | Child that imports the handlers |
  |---|---|---|
  | `fork` | 7.7 ms | 574.7 ms |
  | `forkserver` | 122.0 ms | 635.5 ms |
  | `spawn` | 438.7 ms | 889.1 ms |

  Importing the handlers (mostly reportlab) costs ~0.45–0.57 s. A fresh `spawn`
  per job would add ~0.9 s to a CSV job whose median from acceptance to done was
  33–40 ms in Phase 17's 10- and 100-user runs. `fork` from a parent that has already imported the
  handlers should be close to the 7.7 ms empty-child figure. But forking a process
  that has live threads (the heartbeat thread, pika's I/O) copies any lock those
  threads hold in whatever state it's in, so the child can deadlock on it.
- **Resource isolation:** separate memory, and a crash contained to the child.
  Limits (RLIMIT_AS, CPU time) become possible per child.
- **Implementation complexity:** moderate. Starting, killing and reaping the
  child, the pipe protocol, the outcome mapping, and tests for each path. The
  standard library's `ProcessPoolExecutor` can't kill one stuck task without
  breaking the whole pool, so this is hand-managed `multiprocessing`.

## C. Other approaches this architecture suggests

**C1. A watchdog that ends the worker process.** A thread like the heartbeat
thread checks a deadline. When it passes, the thread records the attempt as timed
out through its own short database session (conditional on the job still being
`RUNNING`), then calls `os._exit(1)`. Everything after that is Phase 15 crash
recovery, which is already built and tested: the broker redelivers the unacked
message, and Compose restarts the container (`restart: unless-stopped`).
Recording the attempt *before* exiting is essential. The reaper deliberately
charges nothing for an interrupted attempt, so without it a job that always hangs
would loop forever. Cost: a container restart per timeout, and the blast radius is
the whole worker process, which is one job at `prefetch_count=1`.

**C2. A cooperative deadline** (a soft limit). Put `deadline` in `JobContext`, and
have handlers check it inside their loops (CSV rows, PDF paragraphs) and raise at
a clean point. Safe and cheap, but only as reliable as each handler's discipline,
and useless against a call that blocks. It complements a hard limit rather than
replacing one.

**Rejected: stop heartbeating after a maximum runtime** so the reaper requeues
the job. The stuck worker would still be running it, so it would execute twice,
and the stuck worker's late `SUCCESS` write isn't conditional on still owning
the job.

## Recommendation for V3

**B, with one long-lived child per worker process that is replaced after a
timeout or crash, plus C2's cooperative deadline as a soft limit.**

- The long-lived child pays the ~0.6–0.9 s start cost once per worker start and
  once per kill, not once per job. No `fork` of a threaded parent is needed.
- The parent keeps the database session, the AMQP channel and the heartbeat. So
  a live heartbeat then also means "the job is within its deadline", and the
  parent can service AMQP while it waits.
- A hard timeout is recorded through `_fail()` as a transient failure, so a job
  that always hangs ends `DEAD` after its retry budget instead of looping.
- Tests to write first: a handler that sleeps past the limit, one that spins in
  pure Python, one stuck inside a C extension call (the case A can't handle),
  and one that crashes the child. Each should end in the right status with an
  attempt recorded.

C1 is the fallback if B's complexity isn't justified: it's smaller and reuses
recovery that already exists, at the cost of a container restart per timeout.
