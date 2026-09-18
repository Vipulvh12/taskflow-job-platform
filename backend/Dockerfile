# One shared dependency stage. The API and the worker import the same `app`
# package and need identical dependencies, so installing them twice in two
# Dockerfiles would guarantee they drift apart the first time one is updated
# and the other forgotten.
FROM python:3.11-slim AS deps
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM deps AS api
COPY app/ ./app/
COPY alembic.ini .
COPY alembic/ ./alembic/
# Migrations run as part of startup rather than as a step someone has to
# remember. `alembic upgrade head` against an already-current schema is a no-op,
# not an error, so this stays correct on every restart.
#
# --host 0.0.0.0 is required, not stylistic: uvicorn's default 127.0.0.1 only
# accepts connections from inside the container's own network namespace, so the
# published port would reach nothing.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]


FROM deps AS worker
COPY app/ ./app/
COPY worker/ ./worker/
# Module form, so /app is on sys.path and both `app` and `worker` resolve.
CMD ["python", "-m", "worker.main"]
