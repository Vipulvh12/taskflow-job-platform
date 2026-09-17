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

Phase 9 complete: the backend MVP works end to end for two real job types —
submit over HTTP, queue, execute, retry with backoff, dead-letter, read results
back.

Still to come: the React frontend (10–11), a test suite (12), and Compose-ing
the API and worker themselves (13).

## Running it

Three processes: the infrastructure containers, the API, and at least one
worker.

```bash
docker compose up -d                          # postgres, redis, rabbitmq
cd backend && uvicorn app.main:app --reload --port 8000    # terminal 1
cd backend && python -m worker.main                         # terminal 2
```

The worker must run as a module (`python -m worker.main`, from `backend/`), not
as a script — that's what puts `backend/` on `sys.path` so both `app` and
`worker` resolve.

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
- **Ack unconditionally** — `SUCCESS` and `FAILED` are both outcomes already
  committed. Redelivery is only wanted when the worker *process* dies mid-job,
  and then the ack line is never reached, so the broker's requeue-on-disconnect
  covers it with no explicit nack logic.
- **Handler exceptions are caught per job** — one bad payload must not kill the
  consume loop.

No auto-reconnect: a consuming connection notices a dead broker immediately
(heartbeat frames), so it surfaces as a crash rather than the silent stale
handle the API's idle connection suffers. Process restart is the right fix —
`restart: unless-stopped` in Phase 13.

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
                   [jobs.dlx]└───┤ jobs.retry.5s   ttl=5s   │
                         │       │ jobs.retry.25s  ttl=25s  │
                         ▼       └──────────────────────────┘
                   [ jobs.dlq ]              ▲
                                worker publishes here with
                                routing key retry.<delay>
```

Backoff is held by the **broker**, not by a sleeping worker: a failed job is
republished onto a delay queue whose `x-message-ttl` expires it back into the
main queue. A worker that slept through its own backoff would be occupying a
process doing nothing.

**One queue per delay tier, not per-message TTL.** RabbitMQ only expires
messages at the *head* of a queue, so a 5s message queued behind a 25s message
waits the full 25s. Per-queue TTL means every message in a queue shares a
deadline and FIFO expiry is correct.

Topology is declared by one shared `declare_topology()`
([queue_topology.py](backend/app/queue_topology.py)) that the API and the worker
both call — RabbitMQ requires every declaration of a queue to pass identical
arguments, so two modules declaring `jobs` independently is a
`PRECONDITION_FAILED` waiting to happen.

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
`JOB_RETRY_DELAYS` (default `5,25`). Adding a tier requires the retry queue for
it to exist — it's declared from the same setting, so both the API and worker
must be restarted together.

### Attempt history

`job_attempts` holds one row per try with its own error and timing, so retry
history is real rather than a counter. Note that the worker *republishes* a new
message for each retry rather than forwarding the original, so the broker's
`x-death` headers only describe the last hop — `job_attempts` is the audit
trail, not the message.

### Known gaps

- A worker killed mid-job leaves that job at `RUNNING` forever — nothing detects
  an abandoned job until V2 heartbeats. Recover by hand:
  `UPDATE jobs SET status='QUEUED' WHERE id='...'` and republish, or resubmit.
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
│   └── tests/
├── frontend/         # React dashboard
├── docker-compose.yml
├── .env.example
└── README.md
```
