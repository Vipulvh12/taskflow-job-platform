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

Phase 4 complete: infrastructure containers, a versioned schema (`users`, `jobs`,
`job_attempts` via Alembic), a FastAPI app with a unified error envelope, and
working authentication. No job submission yet; the `api` and `worker` services
get added to Compose in Phase 13 / Phase 7.

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
admin-only routes will chain `require_admin` on top of it.

**Known limitation (V2):** revocation deletes the Redis key, so a naturally
expired refresh token and a replayed already-rotated one are indistinguishable
— both are just "key not found". Real theft detection needs a short-lived
blocklist of revoked `jti`s instead of deletion, so reuse-after-rotation can
force a full logout.

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
