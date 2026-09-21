# TaskFlow — Distributed Job Processing Platform

Clients submit computational jobs over an HTTP API; the API hands them to a message
queue instead of running them inline; a pool of stateless workers pulls jobs off that
queue and executes them. Results and state live in PostgreSQL. Redis holds the fast,
ephemeral state (idempotency keys, rate limiting, later: worker heartbeats).

## Stack

| Layer | Choice |
|---|---|
| API | Python 3.11 + FastAPI |
| Frontend | React |
| Database | PostgreSQL 16 |
| Queue | RabbitMQ 3 (management image) |
| Cache / ephemeral state | Redis 7 |
| Orchestration | Docker Compose |
| Tests | Pytest |

## Status

**MVP complete (Phases 1–14), V2 in progress.** The whole system runs from
`docker compose up`, is covered by a 99-test suite against real infrastructure,
has benchmarked indexes, recovers automatically from a worker dying mid-job
(Phase 15), and gives admins a dead-letter view with manual retry (Phase 16). It
has been load tested to 1,000 concurrent users with 0% failures, after the load
test found and fixed a deadlock that froze the API at 100 (Phase 17). Request
bodies are capped at 128 KiB, and a GitHub Actions workflow lints, checks
migrations, tests and builds on every push.

## Running it

Everything but the frontend runs in Compose:

```bash
docker compose up --build -d     # postgres, redis, rabbitmq, api, worker
cd frontend && npm run dev       # the frontend still runs on the host
```

The API container runs `alembic upgrade head` before starting uvicorn, so the
schema is current on every boot with nothing to remember. Migrating an
already-current database is a no-op, not an error.

### Running the backend on the host instead

`.env` holds the **Docker-network** hostnames (`postgres`, `redis`, `rabbitmq`)
because that is what a container on the Compose network needs. `.env.host` keeps
the `localhost` equivalents for running the API or worker directly:

```bash
cd backend
cp ../.env.host ../.env            # or point env_file elsewhere
uvicorn app.main:app --reload --port 8000
python -m worker.main
```

`.env.test` stays on `localhost` regardless — pytest runs on the host, not in a
container.

### Startup ordering

Every datastore has a healthcheck, and `api`/`worker` wait on
`condition: service_healthy` for all three. Bare `depends_on` only waits for a
container to *start*, not for the service inside it to accept connections.

RabbitMQ's healthcheck is `check_port_connectivity`, **not** `ping`. `ping`
reports success as soon as the Erlang node is up — roughly 50 seconds before the
AMQP listener accepts connections on this machine — and a worker started on that
false positive dies immediately with `IncompatibleProtocolError`. Observed:
RabbitMQ was `Up 40 seconds (healthy)` when the worker was `Up 1 second`, so the
worker really did wait for the listener.

## The worker

Consumes `{"job_id": ...}`, re-reads the row from Postgres, dispatches on
`job.type` through a decorator registry (`@register("csv_process")`), and
records the outcome as both a `jobs` status change and a `job_attempts` row.

- **`prefetch_count=1`** — one unacked message at a time. Without it a single
  worker buffers a batch and a slow job starves messages sitting in its private
  queue while sibling workers idle.
- **Idempotent dispatch** — a job already `SUCCESS` or `DEAD` is skipped and
  acked. At-least-once delivery means redelivery is normal (worker crash,
  connection drop between commit and ack); without this guard the handler would
  run twice.
- **The message's fate is decided separately from the job's.** `process_job`
  returns a `Disposition`: `ACK` for a finished or skipped job, `RETRY` to
  republish onto a delay queue, `DEAD_LETTER` to `nack(requeue=False)` into the
  DLQ. Keeping AMQP out of `process_job` is also what lets the tests exercise it
  with no channel and no mock. A worker that dies mid-job never reaches its
  ack/nack, so the broker's requeue-on-disconnect covers the one case where
  redelivery is wanted.
- **Handler exceptions are caught per job** — one bad payload must not kill the
  consume loop.

No auto-reconnect: a consuming connection notices a dead broker immediately
(AMQP heartbeat frames), so it surfaces as a crash rather than the silent stale
handle the API's idle connection suffers. Process restart is the right fix —
`restart: unless-stopped` in Phase 13. Those AMQP heartbeats are a different
mechanism from the per-job heartbeats below: they detect a dead *connection*,
not an abandoned *job*.

### Job types

| Type | Payload | Result |
|---|---|---|
| `csv_process` | `csv_text` | `row_count`, `column_count`, per-column stats (`min`/`max`/`mean` for fully numeric columns, `non_null_count` otherwise) |
| `pdf_generate` | `title`, `body`, `author?` | `file_name`, `size_bytes`, `page_count`, `paragraph_count`, `title` |

Payloads travel inline under the existing 64KB cap. `pdf_generate` writes a real
PDF (reportlab) to `storage_dir/<job_id>.pdf` — named by job id so a retry
overwrites its own previous output instead of orphaning files. The result stores
the **file name**, not an absolute path, because that path differs between the
host and a container. Real file *uploads* and object storage stay deferred to V2.

### Adding a job type

Three edits, none of them to the dispatch machinery:

1. a member in `JobType` ([core/enums.py](backend/app/core/enums.py))
2. a payload model in [job_types.py](backend/app/job_types.py), registered in
   `JOB_PAYLOAD_SCHEMAS`
3. a handler module in `worker/handlers/` decorated with `@register("...")`

Handlers are **auto-discovered** — `load_handlers()` imports every module in the
package, so there is no import list to forget. A handler receives
`(payload, JobContext)`, where the context carries `job_id`, `attempt_number`
and `storage_dir`; handlers never reach for settings directly, which keeps them
callable from a test without a database.

The worker logs its registered handlers at startup and warns about any job type
the API accepts that it cannot run — a partial rollout should be visible
immediately, not one dead-lettered job at a time.

### Payload validation happens twice, on purpose

The API validates `payload` against the type's schema at submission (`422`, with
`extra="forbid"` so a typo'd key is rejected rather than silently dropped), and
the handler re-checks its own inputs. The second check is not redundant: a row
can be edited directly in the database, and a replayed message may predate a
schema change. Verified by inserting a malformed row straight into Postgres and
publishing for it — the handler rejects it cleanly as a permanent failure.

## Retries and dead-lettering

```
   (api) ──publish──> [ jobs ] <──consume── (worker)
                         │  ▲
      nack(requeue=      │  │  TTL expiry dead-letters back to the
       False) when       │  │  default exchange, routing key "jobs"
      retries run out    │  │
                         ▼  │   ┌──────────────────────────┐
                   [jobs.dlx]└───┤ jobs.retry.1    ttl=5s   │
                         │       │ jobs.retry.2    ttl=25s  │
                         ▼       └──────────────────────────┘
                   [ jobs.dlq ]              ▲
                                worker publishes here with
                                routing key retry.<tier>
```

Backoff is held by the **broker**, not by a sleeping worker: a failed job is
republished onto a delay queue whose `x-message-ttl` expires it back into the
main queue. A worker that slept through its own backoff would be occupying a
process doing nothing.

**One queue per tier, not per-message TTL.** RabbitMQ only expires
messages at the *head* of a queue, so a 5s message queued behind a 25s message
waits the full 25s. Per-queue TTL means every message in a queue shares a
deadline and FIFO expiry is correct.

Topology is declared by one shared `declare_topology()`
([queue_topology.py](backend/app/queue_topology.py)) that the API and the worker
both call — RabbitMQ requires every declaration of a queue to pass identical
arguments, so two modules declaring `jobs` independently is a
`PRECONDITION_FAILED` waiting to happen.

Note that `jobs`'s arguments are now part of its permanent identity: queue
arguments are immutable, so any future edit to that declaration makes it
inequivalent to the live queue and every declare fails until the queue is
deleted — discarding whatever it holds.

### Failure taxonomy

| Failure | Job status | Attempts | Reaches DLQ? |
|---|---|---|---|
| `HandlerError` (handler rejected its input) | `DEAD` | 1 | yes |
| No handler registered for the type | `DEAD` | 1 | yes |
| Any other exception | `RETRYING` → … → `DEAD` | 3 | yes |
| Queue publish failed at submission | `FAILED` | 0 | no |

**A job that ran and died always rests at `DEAD`.** `FAILED` means the job never
ran at all — the Phase 6 case where the row committed but the publish failed, so
no worker ever saw it. One status and one queue to look at for failed work;
`attempt_count = 0` still distinguishes the never-ran case.

Permanent failures skip the retry ladder. A `HandlerError` means the payload is
wrong, and the payload is immutable — the same input would be rejected
identically three times, 30 seconds apart. A missing handler is the same story:
this worker's registry is fixed for its lifetime, so attempts 2 and 3 look up the
same missing key. If the handler is merely undeployed, the job is recoverable
from the DLQ once it ships, which is the same remedy minus three wasted attempts.

Note the two vocabularies are separate and always were: `job_attempts.status` is
attempt-level (`SUCCESS`/`FAILED`) and records what happened on that try, while
`jobs.status` records where the job as a whole came to rest. A `DEAD` job's
attempt rows all read `FAILED`.

Tune with `JOB_MAX_ATTEMPTS` (default 3, counting the first try) and
`JOB_RETRY_DELAYS` (default `5,25`). Retry queues are named by **tier**
(`jobs.retry.1`, `jobs.retry.2`), not by delay, because the delay is
configurable and a queue called `jobs.retry.5s` would start lying the moment
that setting changed.

Changing `JOB_RETRY_DELAYS` does **not** retune an existing queue —
`x-message-ttl` is immutable after declaration. Delete the `jobs.retry.*`
queues and let them be redeclared, or the new setting is silently ignored.

With the defaults, a job that exhausts its retries takes **~31s** to reach
`DEAD` (5s + 25s of backoff). That's the expected duration of a failing
round-trip, not a hang.

### Attempt history

`job_attempts` holds one row per try with its own error and timing, so retry
history is real rather than a counter. Note that the worker *republishes* a new
message for each retry rather than forwarding the original, so the broker's
`x-death` headers only describe the last hop — `job_attempts` is the audit
trail, not the message.

### Broker durability across `docker compose down`

Queued and dead-lettered messages survive the containers being recreated, not
just restarted. That took two things, and the first alone was not enough:

1. **A named volume** (`rabbitmq_data`) on `/var/lib/rabbitmq`. Without it the
   image's own `VOLUME` makes an anonymous one, and a recreated container gets a
   new, empty anonymous volume.
2. **A fixed `hostname: rabbitmq`.** RabbitMQ names its node `rabbit@<hostname>`
   and keeps data under `mnesia/rabbit@<hostname>`; a container's hostname
   defaults to its container ID, which changes on every recreate. With the volume
   but no fixed hostname, the new container booted as a *new node* and started an
   empty broker right beside the old node's data — which was still on the volume,
   just never read. The first attempt at this fix failed exactly that way, and
   stranded a queued job.

Persistence is also a property of each **message**, not just the queue: a durable
queue loses a transient message on restart. Every publish in the app sets
`delivery_mode=2`, and dead-lettering keeps it, so dead-lettered jobs persist too.

### Crash recovery: heartbeats and the reaper

A worker that dies mid-job is recovered automatically, in about 20 seconds.

**Every job has exactly one owner at a time**, enforced by conditional `UPDATE`s
rather than coordination:

| Transition | Owner |
|---|---|
| `QUEUED`/`RETRYING` → `RUNNING` | a worker, via an atomic claim |
| `RUNNING` → `SUCCESS`/`DEAD`/`RETRYING` | the worker that claimed it |
| `RUNNING` → `QUEUED` | the reaper, only once the job's heartbeat has expired |

The claim is what makes recovery safe. When a worker dies, RabbitMQ *already*
redelivers its unacknowledged message — and the reaper then publishes a second
one. Two independent recovery paths for one failure would otherwise run the job
twice, and for a `RETRYING` job skip its backoff and burn an extra attempt.
Instead, whichever message arrives while the job is still `RUNNING` is skipped,
and only one claim can ever win.

(An earlier version of this README said a job killed mid-run "sits RUNNING
forever". That was never quite true — broker redelivery re-ran it once a worker
came back. What heartbeats and the claim add is doing so without the risk of a
second, concurrent execution.)

- **Heartbeats** are `job_heartbeat:{job_id}` in Redis, 15s TTL, renewed every 5s
  from a background thread, holding the owning worker's id. Keyed by *job* so
  the reaper does one pipelined `EXISTS` per job — keying by worker would force
  a trailing-wildcard `SCAN` across the whole keyspace for every running job.
- **The reaper** (`python -m worker.reaper`, its own Compose service) scans every
  10s for `RUNNING` jobs claimed more than one TTL ago with no heartbeat. It
  leaves `attempt_count` alone: an attempt cut off by a crash was never the job's
  fault and must not eat the retry budget.
- **`started_at IS NOT NULL` defines "abandoned".** A worker's claim always sets
  it, so a `RUNNING` row without one was never picked up by a worker and there is
  no crashed run to recover. It also protects the ~33,000 seeded `RUNNING` rows
  from Phase 14 — without it, the first scan would have requeued every one.

Demonstrated end to end by killing a deliberately slowed worker mid-job:

```
t+ 0.0s  worker killed mid-job
t+ 2.6s  restarted worker gets the broker's redelivery -> skips it (job still RUNNING)
t+13.2s  heartbeat expires
t+19.0s  reaper requeues it; worker claims and completes it
         status=SUCCESS  attempt_count=1  attempt_rows=1
```

### Known gaps

- **Heartbeats prove the process is alive, not that the job is progressing.** A
  handler stuck forever keeps its heartbeat thread beating and is never reaped.
  Catching that needs a per-job execution timeout.
- Scaling the reaper is safe, but its healthcheck only *reports*: Compose marks
  a reaper that stops completing scans `unhealthy` (verified — within ~50s of
  its scans starting to fail) and does not restart it. Acting on that needs an
  orchestrator or an autoheal sidecar.
- Each reaper scan currently reads ~3,100 pages (29 ms), because it must check
  `started_at` on the ~33,000 seeded `RUNNING` rows. With real traffic the number
  of `RUNNING` jobs is roughly the number of workers, so this is an artifact of
  the seed data rather than a production cost.
- If the worker cannot publish the retry message, it requeues instead of acking.
  The job gets re-attempted, costing one extra attempt — acceptable under
  at-least-once, and better than a job stranded at `RETRYING` with no message
  anywhere.
- Backoff has no jitter, so a batch of jobs failing together retries in
  lockstep. Fine at this scale; jitter is the fix when it isn't.

## Auth

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /auth/register` | — | Create an account, returns a token pair |
| `POST /auth/login` | — | Exchange credentials for a token pair |
| `POST /auth/refresh` | — | Rotate a refresh token into a new pair |
| `POST /auth/logout` | — | Revoke a refresh token (204) |
| `GET /auth/me` | Bearer | The caller's own user record |

Passwords are hashed with bcrypt (called directly, not via the unmaintained
`passlib`). Access tokens are stateless 15-minute JWTs verified by signature
alone. Refresh tokens last 7 days and are **revocable**: each carries a `jti`
recorded in Redis as `refresh_token:<jti> -> user_id` with a matching TTL, so
logout is real revocation rather than the client forgetting a string. Refreshing
rotates — the presented token's `jti` is deleted and a brand-new pair issued.

Protected routes depend on `get_current_user` (in [deps.py](backend/app/deps.py));
admin-only routes will chain `require_admin` on top of it. Authorization reads
`is_admin` from the database, not from the token's claim, so a demotion takes
effect on the next request instead of when the access token expires.

**Known limitation (V2):** revocation deletes the Redis key, so a naturally
expired refresh token and a replayed already-rotated one are indistinguishable
— both are just "key not found". Real theft detection needs a short-lived
blocklist of revoked `jti`s instead of deletion, so reuse-after-rotation can
force a full logout.

## Jobs

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | Submit a job. `201` if created, `200` if an idempotency key replayed an existing one |
| `GET /jobs/{id}` | Full record incl. attempt history. Someone else's job reads as `404` |
| `GET /jobs` | The caller's jobs, filterable by `status`/`type`, paginated via `page`/`page_size` (max 100) |

```jsonc
// POST /jobs
{
  "type": "csv_process",        // csv_process | pdf_generate
  "payload": {"rows": 42},      // any JSON object, max 64KB
  "priority": 5,                // 0-10, HIGHER = more urgent (matches RabbitMQ)
  "idempotency_key": "abc-123"  // optional; also accepted as an Idempotency-Key header
}
```

### Idempotency

The guarantee is the `uq_jobs_user_idempotency_key` constraint, not the cache.
Redis holds `idempotency:<user_id>:<key> -> job_id` for 24h purely to skip a
doomed INSERT; wipe Redis entirely and behaviour is unchanged, because a
duplicate still raises `IntegrityError` and the existing row is returned. Keys
are scoped per user, so two users may use the same key.

Replaying a key with a **different** `type`, `payload` or `priority` is a `409`,
not a silent replay — otherwise the second job would never run and the client
would never find out. Sending different keys in the body and the
`Idempotency-Key` header is a `400`.

### Notes / limitations

- List rows omit `payload` and `result`; fetch the detail endpoint for those.
- Pagination is offset-based, which is fine at this size but drifts under
  concurrent inserts and degrades on deep pages. Keyset pagination is the fix
  when it matters (Phase 14).
- Per-type `payload` schemas are deliberately not defined yet — the handler that
  consumes a payload defines its contract, and those land in Phases 7 and 9.
  Today `payload` is validated as "a JSON object under 64KB".
## Queueing

A committed job is published to the durable `jobs` queue as a persistent
message carrying **only** `{"job_id": "..."}`. Postgres is the single source of
truth for job data; the message is a signal that something needs processing, and
the worker re-reads the row by id. Copying `type`/`payload` into the message
would let a worker act on data that changed after publish (an admin retry, say).

The broker connection is one shared `pika.BlockingConnection` behind a
`threading.Lock` — pika connections are not thread-safe and sync routes run
across FastAPI's threadpool. A connection per publish would add a TCP + AMQP
handshake to every submission. Under heavy concurrency this lock serializes
publishes; a pool is the answer if that ever shows up in a load test.

The connection opens on first publish, not at startup, so the API and broker
have no startup-order dependency (which matters once both are Compose services
in Phase 13). A publish retries **once** on a fresh connection before failing:
pika only notices a broker that went away when it next touches the socket, so
`is_closed` still reads `False` and the cached channel is quietly dead. Without
the retry, the first job submitted after any broker restart is sacrificed to
discovering that.

### Dual-write limitation

The job row and the queue message live in two systems with no shared
transaction. If the commit succeeds but the publish fails, the job is marked
`FAILED` and the client gets a `502` — visibly broken beats a job sitting at
`QUEUED` forever with nothing to process it. Such a row is distinguishable from
a genuine processing failure by `attempt_count = 0`. The real fix is a
transactional outbox (write the "needs publishing" fact in the job's own
transaction, let a relay publish it with retries); that's V2/V3.

Note also that `basic_publish` here is fire-and-forget — there are no publisher
confirms, so a message the broker drops after accepting the TCP write would go
unnoticed. Confirms are the hardening step that closes that gap.

## Rate limiting

| Endpoint | Limit | Keyed by |
|---|---|---|
| `POST /jobs` | 100 / 60s | authenticated user |
| `POST /auth/login` | 10 / 60s | client IP |
| `POST /auth/register` | 10 / 60s | client IP |

Exceeding a limit returns `429` in the usual envelope (`"code": "rate_limited"`)
plus a `Retry-After` header holding the seconds left in the window. Read
endpoints are unlimited — they aren't the abuse surface here.

Login and register are keyed by IP because a brute-force attacker has no session
yet; that is the whole point of the attack. The limiter is a dependency, so it
runs *before* the credential check — the 11th attempt is rejected whether or not
the password is correct.

Implementation is a fixed-window counter in `_check_rate_limit`
([deps.py](backend/app/deps.py)). `INCR` and `EXPIRE … NX` go out in one
pipeline: the naive `if INCR == 1: EXPIRE` version leaves a key incremented with
no TTL if the process dies between the two calls, locking that caller out
permanently until someone deletes the key by hand. `NX` also stops concurrent
requests from pushing the expiry outward.

**Known limitations:**
- Fixed windows allow a ~2x burst across a boundary (100 requests at 0:59 plus
  100 at 1:00). A sliding-window log or token bucket fixes it, at real cost.
- `request.client.host` is the direct TCP peer. Correct while Uvicorn is exposed
  directly; behind a reverse proxy every request would look like it came from
  the proxy, and this would need a trusted `X-Forwarded-For` instead.

Reset counters during development with
`docker exec taskflow-redis-1 redis-cli FLUSHDB` (also clears idempotency keys
and refresh tokens).

### Benchmarking gotcha

Measure against `http://127.0.0.1:8000`, not `http://localhost:8000`. Uvicorn
binds IPv4 only, and `localhost` resolving to `::1` first adds a ~2000 ms
connect stall per request on Windows — enough to swamp any real measurement.
`POST /jobs` is ~18 ms; the same call via `localhost` "measures" ~2070 ms.

### Error responses

Every error the API can produce — business-logic errors, request validation
failures, framework `HTTPException`s, and unhandled bugs — comes back in one
shape, so the frontend needs exactly one error-parsing path:

```json
{"error": {"code": "not_found", "message": "Job 123 does not exist."}}
```

`code` is stable and machine-readable (`not_found`, `conflict`, `unauthorized`,
`forbidden`, `validation_error`, `http_error`, `internal_error`); branch on it
rather than on `message`.

## Local setup

Requires Docker + Docker Compose, Python 3.11+, Node 18+.

```bash
cp .env.example .env      # .env is gitignored; .env.example holds no real secrets
docker compose up -d
docker compose ps
```

Then edit `.env` to point at the **published host ports** rather than the
Docker-network hostnames — the backend runs on your machine until Phase 13:

```
DATABASE_URL=postgresql://taskflow:taskflow_dev_password@localhost:5433/taskflow
REDIS_URL=redis://localhost:6379/0
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
```

> Postgres is published on host port **5433**, not 5432, because a natively
> installed PostgreSQL service already owns 5432 on this machine. Inside the
> Compose network the port is still 5432, which is why `.env.example` keeps
> `postgres:5432`.

### Backend environment

```bash
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1          # PowerShell; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
alembic upgrade head                 # run from backend/
```

### Verify the infrastructure

```bash
docker exec taskflow-postgres-1 psql -U taskflow -d taskflow -c "\dt"   # 4 tables
docker exec taskflow-redis-1 redis-cli ping                              # -> PONG
```

RabbitMQ management UI: http://localhost:15672 (guest / guest).

### Migrations

All Alembic commands run from `backend/`:

```bash
alembic revision --autogenerate -m "describe the change"   # create a migration
alembic upgrade head                                        # apply
alembic downgrade -1                                        # roll back one
alembic current                                             # what's applied
alembic check                                               # models vs DB drift
```

### Run the API

From `backend/`, with the venv active:

```bash
uvicorn app.main:app --reload --port 8000
```

- http://localhost:8000/health → `{"status":"ok"}`
- http://localhost:8000/docs → Swagger UI

### Shut down

```bash
docker compose down        # keeps the postgres_data volume
docker compose down -v     # also drops the database volume
```

## Layout

```
taskflow/
├── backend/
│   ├── app/          # FastAPI application
│   │   ├── config.py     # env-driven settings
│   │   ├── db.py         # SQLAlchemy engine/session
│   │   └── models/       # User, Job, JobAttempt
│   ├── alembic/      # migration environment + versions/
│   ├── worker/       # queue consumer + job handlers
│   ├── loadtest/     # Locust scenario + server-side sampling (Phase 17)
│   ├── scripts/      # benchmark seeding and report rendering
│   ├── benchmarks/   # Phase 14 and 17 results, generated from raw data
│   └── tests/
├── frontend/         # React dashboard
├── docs/             # design notes (per-job timeout)
├── .github/workflows/ci.yml
├── docker-compose.yml
├── .env.example
└── README.md
```

## Frontend

React 19 + Vite, talking to the API at `VITE_API_BASE_URL` (default
`http://localhost:8000`). Routes: `/login`, `/register`, and `/jobs` behind a
`ProtectedRoute` that redirects to `/login` when there is no user.

### Tokens are held in memory, never in storage

`localStorage` is readable by any script on the page, so one successful XSS
anywhere — a dependency, a stray `dangerouslySetInnerHTML` — exfiltrates every
stored token at once. Module-level variables in
[api/client.js](frontend/src/api/client.js) mean an attacker's script only
reaches what is in this tab's heap while it runs.

**The cost is real: a hard refresh logs the user out.** Not a bug, an accepted
MVP trade. The production fix is an `httpOnly` refresh cookie (invisible to JS)
with CSRF protection — V2/V3, alongside the other deferred auth hardening.

### One `apiFetch`, three jobs

Every protected call needs the same things, so they live in one place rather
than in each component: attach `Authorization: Bearer …`, turn the API's
`{"error": {"code", "message"}}` envelope into a typed `ApiError`, and on a
`401` attempt exactly one silent refresh before giving up.

Auth endpoints are excluded from that retry — refreshing on a failed refresh
would loop, and retrying a login would re-present the credential that just
failed.

Concurrent 401s share a **single** in-flight refresh. Without that, two
simultaneous expired-token calls would each fire a refresh; the first rotates
the token (Phase 4 rotates on every refresh), so the second presents one the
server has already revoked and logs the user out for no reason.

### CORS

The API allows `CORS_ORIGINS_RAW` (default `http://localhost:5173` and the
127.0.0.1 equivalent). No `allow_credentials` — that flag is for cookie auth,
and tokens travel in the `Authorization` header. If Vite picks a different port
because 5173 is taken, set `CORS_ORIGINS_RAW` to match; a mismatch surfaces only
as an opaque browser CORS error with nothing in the server log.

### Job pages

| Route | Purpose |
|---|---|
| `/jobs` | The caller's jobs, newest first, paginated |
| `/jobs/new` | Submit form; the type dropdown drives which payload fields render |
| `/jobs/:id` | Full record — payload, result, and one row per attempt |

**Polling, not WebSockets.** Jobs finish in seconds, so a persistent connection
would buy imperceptible latency in exchange for reconnect handling and
auth-over-a-socket. Both pages poll every 2s.

Polling is **derived state, not a one-way stop**: it runs exactly while
something on the page is non-terminal (`hasActiveJobs` in
[job-status.js](frontend/src/job-status.js)). A finished page stops hitting the
API, and polling resumes by itself when a newly submitted job appears — a
`clearInterval` that never restarts would leave a stale page silent forever.

`FAILED` counts as terminal for polling even though the job never ran: it means
the queue publish failed at submission, so nothing will ever move it.

### The duplicated contract

[job-types.js](frontend/src/job-types.js) mirrors the payload models in
`backend/app/job_types.py` by hand. That is deliberate duplication — the
alternative is a `GET /job-types` endpoint the spec never asked for — and the
cost is that a backend schema change needs a matching edit there.

It fails loudly rather than subtly: the backend's payload models use
`extra="forbid"`, so a drifted field name is a `422` naming the offending key,
not a silently dropped value.

## Tests

```bash
cd backend
docker exec taskflow-postgres-1 psql -U taskflow -d taskflow -c "CREATE DATABASE taskflow_test;"  # once
pytest -q
```

99 tests, ~110s, against **real** infrastructure rather than mocks. That is a
deliberate choice: the two hardest bugs in this project so far — the idempotency
race and the stale broker connection — both lived precisely in behaviour a mock
would have faked away.

### Isolation

`.env.test` (loaded by `pytest-dotenv`, so `config.py` is untouched) points the
suite at a separate database `taskflow_test`, Redis **db index 1** rather than
0, and a throwaway `jobs.test` queue. An autouse fixture truncates, flushes and
purges before *every* test, so there are no ordering dependencies — verified by
running the suite twice back to back with no cleanup in between.

`conftest.py` refuses to start at all unless it is pointed at `taskflow_test`
and Redis db 1. The cleanup fixture runs `TRUNCATE`, so a missing `.env.test`
would otherwise silently destroy the dev database.

Tests never touch the real `jobs` queue. Its declaration carries Phase 8's
dead-letter arguments, and RabbitMQ rejects any declare whose arguments differ —
so one careless `queue_declare` in a test would take the queue down for
everything. `publish_job` takes a `queue_name` for that reason.

### What is covered

| Area | Notable cases |
|---|---|
| Security (unit) | hash/verify round-trip, per-call salting, expired token, token forged with another secret |
| Handlers (unit) | real CSV stats, real PDF bytes on disk, permanent-failure signalling, retry overwriting its own output |
| Auth | rotation, replay-after-rotation, revocation, token type confusion, account enumeration, the 72-**byte** password limit |
| Idempotency | replay, header form, conflicting keys, different body → 409, per-user scoping, **8-thread race**, and survival of an empty cache |
| Rate limiting | exact 100/1 and 10/1 splits, `Retry-After`, TTL always set, per-user isolation, limiter running *before* the credential check |
| Worker lifecycle | success, permanent vs transient failure, both backoff tiers, exhaustion → `DEAD`, attempt history, duplicate-delivery skip, missing row |
| Heartbeats & reaper | atomic claim (two deliveries run once), heartbeat outliving its TTL, compare-and-delete, abandoned job requeued, grace period for a fresh claim, conditional requeue, failed republish |
| Admin | 403/401, ordering and pagination covering every row once, fresh budget with continuous numbering, 409/404, concurrent retries, failed publish leaves the job `DEAD` |
| Concurrency | 100 simultaneous requests — more than the pool plus the threadpool — complete without the Phase 17 deadlock |
| Request-body limit | largest legitimate submission accepted, exact-limit vs limit+1, a body the endpoint never reads, 30 MB login rejected before its rate limiter runs, and via raw ASGI: 0 bytes read with `Content-Length`, at most one chunk past the limit when chunked |

The worker tests call `process_job` directly against the test database rather
than running a live consumer: a real one would make the suite wait out 5s + 25s
of backoff per dead job and assert on wall-clock gaps. `process_job` returns a
`Disposition` describing what should happen to the *message*, which is the seam
that makes this possible with no channel and no mock of one.

The 8-thread race deliberately does **not** override the app's DB dependency.
Sharing one session between test and handler would make the race meaningless —
SQLAlchemy sessions are not thread-safe, so eight threads through one session is
a crash, not a race. Letting each request open its own session is what puts
eight real connections in contention on the unique constraint.

## Containers

| Service | Image | Notes |
|---|---|---|
| `postgres` | postgres:16 | published on host **5433** (a native Postgres owns 5432 here) |
| `redis` | redis:7 | db 0 for the app, db 1 for tests |
| `rabbitmq` | rabbitmq:3-management | UI at :15672 |
| `api` | built, `target: api` | migrates, then serves on :8000 |
| `worker` | built, `target: worker` | consumes; scale with `--scale worker=N` |

One Dockerfile with a shared `deps` stage and two thin final stages. The API and
worker import the same `app` package and need identical dependencies; installing
them in two separate Dockerfiles would guarantee drift the first time one was
updated and the other forgotten.

### `STORAGE_DIR` must be set explicitly

`settings.storage_dir` defaults to `ROOT_DIR / "storage"`, and `ROOT_DIR` is
derived from `config.py`'s own location — which resolves to `/` inside the
image, giving `/storage`. The volume mounts at `/app/storage`. Left to the
default, the worker would write generated PDFs to a path outside the volume,
where they vanish on restart and the API can never see them. Compose sets
`STORAGE_DIR=/app/storage` on both services.

Verified the volume is genuinely shared: the worker writes a PDF, and the API
container lists the same filename at the same path with the same byte count —
and a file written from the API side is readable by the worker.

### `restart: unless-stopped`

On all six services, after a Docker Desktop auto-update once killed every
container at once with exit 255.

Note this does **not** cover `docker stop` or `docker kill` — Docker skips the
restart policy for containers stopped manually. It covers a container that dies
on its own. Nor does it act on a failing healthcheck: a reaper that stays alive
but stops scanning shows as `unhealthy` in `docker compose ps` and stays that
way until a human or an external tool (an orchestrator, an autoheal sidecar)
restarts it. Demonstrated by stopping RabbitMQ: the worker (which has no
auto-reconnect by design) crash-looped 12 times, then stabilized by itself once
the broker returned, and processed the next job normally.

## Index benchmark

Full write-up with plans and methodology: [benchmarks/phase14_results.md](backend/benchmarks/phase14_results.md).

Measured at 200,000 rows with `EXPLAIN (ANALYZE, BUFFERS)` against the **exact**
SQL the API emits, median of 6 warm runs:

| Query | No index | With Phase 2 indexes | Speedup |
|---|---|---|---|
| Highest-priority queued jobs | 34.33 ms · 3,150 pages | 0.077 ms · 35 pages | **446×** |
| Count of a user's queued jobs | 25.49 ms · 3,078 pages | 0.481 ms · 5 pages | **53×** |
| A user's queued jobs, newest first | 22.58 ms | 2.43 ms | **9×** |
| A user's jobs, newest first (default page) | 24.42 ms | 9.05 ms | 3× |

Three findings the headline numbers hide:

- **The unique constraint's index was already doing half the work.**
  `uq_jobs_user_idempotency_key` leads with `user_id`, so it serves every
  `WHERE user_id = ?`. Dropping `idx_jobs_user_status` didn't produce a seq scan —
  the planner switched indexes. The "no index" column above needed index paths
  disabled for the session to measure honestly.
- **The most frequently run query gets nothing from either Phase 2 index.** The
  default job list — polled every 2s — runs an identical plan with or without
  them, because its cost is in `ORDER BY created_at DESC`, not the `WHERE`. A
  `(user_id, created_at DESC, id DESC)` index took it from **9.05 ms to 0.061 ms
  (148×)**, reading 23 pages instead of 2,980. Added in Phase 14b as migration
  `252ae359c5a3`, which the API container applies itself on startup.
- **The biggest win serves a query nothing runs yet.** `idx_jobs_status_priority`
  is the 446× result, but RabbitMQ orders the worker's jobs, not Postgres. It
  earns its keep once an admin view or scheduler asks for the next queued job.

At the original 447 rows every query was under half a millisecond with or without
indexes — an index only pays once the table is large *and* the predicate
selective.

### The seeded rows are still there

The 199,553 benchmark rows belong to dedicated `bench-NN@taskflow.local` users,
not real accounts — seeded jobs in `QUEUED`/`RUNNING`/`RETRYING` have no queue
message behind them and never settle, so attached to a real account they would
keep its job list polling every 2s. Left in place for V2 load testing. To remove:

```sql
DELETE FROM jobs WHERE user_id IN
    (SELECT id FROM users WHERE email LIKE 'bench-%@taskflow.local');
DELETE FROM users WHERE email LIKE 'bench-%@taskflow.local';
```

## Admin: dead jobs and manual retry

| Endpoint | Purpose |
|---|---|
| `GET /admin/jobs/dead` | `DEAD` jobs across all users, highest priority first, then oldest, with each job's final error |
| `POST /admin/jobs/{id}/retry` | Put a `DEAD` job back in the queue with a fresh retry budget |

Both require `require_admin`, which reads `is_admin` from the database. Admin is
never grantable through the API — only with
`UPDATE users SET is_admin = true WHERE email = '...'`. The frontend's
`/admin/dead` page (linked in the nav for admins only) is presentation; the API
returns `403` to a non-admin regardless.

The view reads `DEAD` rows from **Postgres**, not the `jobs.dlq` queue. Each row
carries `last_error` because the whole decision is *whether* a retry can help: a
job that died on a malformed payload will die again identically.

### A retry is a fresh budget, not a renumbering

A `DEAD` job has spent its budget legitimately, so an admin retry grants a new
one — deliberately unlike the reaper, which rescues an attempt cut off through no
fault of the job and so charges nothing.

The budget is reset by raising **`attempt_base`** to the attempts already spent,
**not** by zeroing `attempt_count`. `job_attempts` has no uniqueness on
`(job_id, attempt_number)`, so zeroing would number the next attempt `1` again
and turn the history into `1, 2, 3, 1, 2, 3`. The worker measures its ladder as
`attempt_number - attempt_base`, so `attempt_count` and the numbering keep
counting. Verified live: a transiently failing job, retried after dying, ran
attempts **4, 5, 6** with the usual 5s/25s backoff and went back to `DEAD`, its
history reading `[1, 2, 3, 4, 5, 6]`. The job detail page marks where the fresh
budget began.

### One requeue path

The reaper (`RUNNING → QUEUED`) and admin retry (`DEAD → QUEUED`) share
`requeue()` in [job_transitions.py](backend/app/services/job_transitions.py): a
conditional transition, a publish, and — if the publish fails — restoring the row
*exactly* as it was. For admin retry that means the job stays `DEAD` with its
original budget and completion time, still visible and retryable; marking it
`FAILED` would hide it from this view and claim a job that ran three times never
ran. Two simultaneous retries of one job produce exactly one `200` and one `409`,
and one publish.

### The index, and why it isn't the Phase 14 number

The dead list is the first real query to use `idx_jobs_status_priority`
(`Index Scan Backward`). It is **not** the 446× shape Phase 14 measured: that
query ordered by `priority` alone, so Postgres stopped after 50 index entries.
This one also orders by `created_at, id`, which the index does not hold, so
Postgres reads the whole top-priority group (3,088 rows here) and runs an
*Incremental Sort* — ~10 ms for a page, plus ~21 ms for the `count(*)` over 33,522
`DEAD` rows. An index on `(status, priority DESC, created_at, id)` would remove
the sort, but unlike 14b this is an occasionally opened admin page, not a query
polled every 2 seconds, so it was measured and left alone.

### Known gap

`jobs.dlq` now only grows. Nothing consumes it, RabbitMQ cannot delete one
specific message, and since Phase 15b it survives recreation — so every
dead-lettered message is kept forever, including for jobs later retried. The
view is unaffected (it reads Postgres), but the queue wants an `x-max-length` or
message TTL, which, being a queue argument, means redeclaring it.

## Load testing

Full write-up, generated from the raw run data:
[benchmarks/phase17_results.md](backend/benchmarks/phase17_results.md).

Locust against the Compose stack over real HTTP. Reads go to the `bench-NN`
accounts that own the Phase 14 rows (~10K jobs each), since a fresh account's
job list is fast with or without an index. Submissions go to dedicated
`loadtest-NN` accounts. Each simulated user waits 1–3 s between requests. Load
generator and server share one 6-core laptop.

| Users | Before the fix | After the fix |
|---|---|---|
| 10 | 0 failed · p50 15 ms · p95 33 ms | 0 failed · p50 15 ms · p95 41 ms |
| 100 | **40 × 500**, a 30 s freeze · p99 31 s | 0 failed · 47 req/s · p50 25 ms · p95 150 ms |
| 1,000 | **93.6% failed** · 13 req/s | 0 failed · 77 req/s · p50 10 s (saturated) |

### The bottleneck at 100 users was a deadlock, not load

At 34 req/s the API froze for 30 s: its CPU dropped to 0.2%, all 15 pooled
connections sat `idle in transaction`, and even `/health` got no answer. Then
exactly 40 requests failed with `QueuePool limit ... timeout 30.00`.

FastAPI runs each sync dependency, the endpoint, and response validation as
separate trips into one 40-thread pool, and a request holds its DB connection
across all of them from `get_current_user` on. Once more than 15 + 40 requests
are in flight, every connection can be held by a request waiting for a thread,
and every thread by a request waiting for a connection. It stays stuck until
`pool_timeout` fails the 40 waiting threads. `loadtest/burst.py` reproduces it
on demand: 50 simultaneous requests pass and 60 wedge.

**A bigger pool doesn't fix it.** At 40 connections the threshold only moves to
80: 100 simultaneous requests wedged the same way. The fix,
[concurrency_limit.py](backend/app/concurrency_limit.py), caps in-flight
requests per process at the pool size. A checkout then never waits and the
cycle can't form. Requests over the cap wait in the event loop holding nothing.
A regression test fires 100 simultaneous requests. Without the cap it fails with
pool timeouts and a 61 s request; with the cap it passes in ~3 s.

### After the fix: performance

**0% failures at every level, including 1,000 users.** Same scenario and
harness throughout:

| API configuration | 100 users | 1,000 users |
|---|---|---|
| 1 Uvicorn process | 47 req/s · p50 25 ms · p99 340 ms | 77 req/s · p50 10 s |
| **2 Uvicorn processes — the current configuration** | 46 req/s · p50 24 ms · p99 2.2 s | **112 req/s · p50 6.4 s** |
| 4 Uvicorn processes — *an experiment only, not deployed* | — | 161 req/s · p50 3.6 s |

- **1,000 users saturate the API; they don't break it.** With one process, the
  API runs at 144% CPU (one core of Python plus the work that runs outside the
  GIL), while Postgres has a median of **0** connections running a query. The
  10 s p50 is queueing, not slow queries: 1,000 users ÷ (10 s + 2 s think time)
  ≈ 83 req/s against 77 measured. More processes raise the ceiling, sublinearly,
  because the laptop's 6 cores are shared with Postgres and Locust.
- **At 100 users the API isn't the constraint.** The request rate is set by the
  simulated users, so the medians barely move between 1 and 2 processes. The p99
  difference (340 ms vs 2.2 s) comes from the unexplained pauses described under
  [Known limitations](#known-limitations-and-future-work), not from the process
  count. One 2 s pause with ~100 requests in flight is enough to set a 2-minute
  run's p99.

### Two API processes, each with its own pool and cap

The API runs **two Uvicorn processes** (`WEB_CONCURRENCY: "2"` in
`docker-compose.yml`, which uvicorn reads as its `--workers` default). Each
process has its **own** 15-connection pool and its **own** in-flight cap of 15,
computed from the same settings. So the deadlock fix holds per process, and the
API uses at most 30 of Postgres's 100 connections, shared with the worker and
reaper.

Verified live, since the pytest regression test runs in-process and can't see
two processes: 60, 100 and 200 simultaneous requests all returned 200, and
connections peaked at 32 under 1,000 users. The image's CMD keeps `exec`, so
Uvicorn is PID 1. Without it, `sh` stays PID 1, `docker stop`'s SIGTERM never
reaches Uvicorn, and it's SIGKILLed with no graceful shutdown (exit 137;
measured).

A py-spy profile of the saturated API puts 41% of GIL time in SQLAlchemy's
per-statement machinery and ~29% in FastAPI and anyio's threadpool dispatch, but
only 1.4% in the Postgres driver. The same list request costs Postgres 2.3 ms,
and the Phase 14b page query is **0.061 ms, unchanged under load**. The
`COUNT(*)` for the page total is now 96% of the request's database time, because
it reads every one of the user's ~10K rows.

The single worker kept up at every level: queue delay p50 was 125 ms at 1,000
users, and every run drained within a second of its last submission.

### Running it

```bash
cd backend
pip install -r requirements-dev.txt                       # Locust stays out of the images
python -m loadtest.seed_loadtest_users                    # once: 50 loadtest-NN accounts
python -m loadtest.run 1000 50 180 u1000                  # Locust + sampler + queue delay → benchmarks/phase17/raw/
python scripts/render_loadtest.py                         # → benchmarks/phase17_results.md
```

Tokens are minted with the app's own `create_access_token` rather than obtained
from `/auth/login`. Login is limited to 10/min per IP, and every simulated user
shares one IP. `*@taskflow.local` also can't log in over HTTP at all: `.local` is
a special-use domain, which `EmailStr` rejects. **Login latency is not measured.**

Load-testing limitations (saturation reported as unhealthy, no load shedding,
the unexplained pauses) are listed under
[Known limitations](#known-limitations-and-future-work).

## Request-body limit

Every request body is capped at **128 KiB** (`MAX_REQUEST_BODY_BYTES`), using
Starlette's `RequestBodyLimitMiddleware`, wired up in
[body_limit.py](backend/app/body_limit.py).

**Why.** Before this, a 30 MB *unauthenticated* `POST /auth/login` was read,
JSON-parsed and validated in full, then rejected with a 422. That took 1.64 s
and added 57 MB of API memory, per request. FastAPI reads the body before any
dependency runs, so the login rate limiter never got a say.

**Why 128 KiB.** The largest legitimate body is a job submission at the 64 KiB
payload cap. Even pretty-printed, with a 255-character idempotency key, it's
65,893 bytes; every other endpoint's body is under ~350 bytes. 128 KiB is about
2× the largest legitimate body. The API refuses to start if the setting is too
small to hold a capped payload.

**How it rejects.** With a `Content-Length` over the limit, the app's first
read of the body fails before a single byte is read. A chunked body is cut off
at the limit plus one chunk. Either way the client gets a 413 in the standard
envelope, `{"error": {"code": "payload_too_large", ...}}`. The library's own
response is plain text, so a small wrapper rewrites it, because the frontend
reads `error.message`. The middleware sits inside CORS, so a browser can read
the 413.

**Verified.** Nine tests. The key one drives the app directly with a body
reader that counts bytes: a 30 MB login with `Content-Length` pulls **0**
bytes, and a chunked one stops within one chunk of the limit. The largest
legitimate submission still succeeds, a body of exactly the limit gets through
to the JSON parser, and one byte more is rejected. With the middleware removed, the 5 over-limit
tests fail. Live, the 30 MB login now gets a 413 in 0.03–0.09 s with API memory
flat. `curl`, which asks the server before uploading a large body (`Expect:
100-continue`), uploaded 0 of its 30,000,038 bytes.

## CI

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push and pull
request, in one job on `ubuntu-latest` with Python 3.11, the Dockerfile's
version:

```
install requirements-dev.txt
  → ruff (lint)
  → alembic upgrade head + alembic check on an empty database (migrations match the models)
  → pytest, the full suite
  → docker build --target api, --target worker
```

- **Real infrastructure, as locally.** The tests run against Postgres 16,
  Redis 7 and RabbitMQ 3 service containers: the `docker-compose.yml` images,
  on the ports `backend/.env.test` expects. Each has a healthcheck, which GitHub
  waits on before running steps.
- **CI's Postgres uses a throwaway password.** `pytest.ini` lets `.env.test`
  override the environment (a safety rail), so the job rewrites `DATABASE_URL`
  in its own checkout. The conftest checks (database `taskflow_test`, Redis db 1)
  still apply.
- **Lint checks for bugs, not style.** `ruff.toml` selects pyflakes plus
  pycodestyle's error checks explicitly, so a ruff upgrade can't change what CI
  enforces. Its first run found four model names used only in `Mapped["..."]`
  annotations, now imported under `TYPE_CHECKING`, and an unused test variable.

**Status: not yet run on GitHub.** This repository has no remote. The workflow
passes `actionlint`, and every step was re-enacted locally in a fresh `git clone`
against service containers started with the workflow's exact images, credentials,
ports and healthchecks: lint clean, 3 migrations applied and no drift, **99
passed**, both images built. Linux-specific details (the runner's shell,
`setup-python`'s cache) can only be confirmed by the first real run.

## Known limitations and future work

- **No per-job execution timeout.** A handler that never returns keeps its
  heartbeat alive, so the reaper never intervenes and the job stays `RUNNING`
  indefinitely. Python can't safely kill a thread from another thread. The
  options, with measured process start costs, are compared in
  [docs/per-job-timeout.md](docs/per-job-timeout.md). The recommendation for V3
  is a long-lived child process per worker that is killed and replaced on
  timeout, plus a cooperative deadline in `JobContext`. Not implemented.
- **Unexplained pauses.** During the 10- and 100-user runs, with 1 and with 2
  processes, the server occasionally paused for 1–2.4 s. Locust and a separate
  `/health` probe both saw them, so they happened on the server. They set the
  p99 at those load levels. **The cause is unknown.** A Postgres checkpoint is
  ruled out (a forced one caused no pause), and memory pressure looks unlikely
  (the VM's memory-stall counter and swap-outs didn't move across a 1,000-user
  run).
- **Saturation looks like ill health.** Past saturation, `/health` waits behind
  the in-flight cap too: ~10 s at 1,000 users, over the healthcheck's 5 s
  timeout. Compose only reports that. An orchestrator that *restarts* on a
  failed liveness check would restart a working, saturated API, so `/health`
  would need splitting into a liveness check (no cap, no database) and a
  readiness check.
- **No load shedding.** Requests over the cap queue without bound. A limit
  answered with `503` is the next step for sustained overload.
- **The reaper's healthcheck only reports.** A reaper that stops scanning shows
  `unhealthy`, and nothing restarts it.
- **`jobs.dlq` only grows** (see the admin section). It needs a length cap or
  message TTL, which means redeclaring the queue.
- **Login throughput is untested.** The load test mints tokens (see above).
- **Single-node datastores.** One Postgres, one Redis and one RabbitMQ, with no
  replication — in scope for a one-host deployment, not beyond it.
- **CI hasn't run on GitHub yet** (see above).
