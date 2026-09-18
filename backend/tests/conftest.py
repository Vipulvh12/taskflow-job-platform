import uuid

import pika
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.main import app
from app.models.base import Base
from app.redis_client import redis_client

TEST_QUEUE = "jobs.test"

# ---------------------------------------------------------------------------
# Safety rail. The cleanup fixture below TRUNCATEs tables, so if pytest-dotenv
# failed to load .env.test for any reason — a missing plugin, a renamed file —
# this suite would silently wipe the development database. Refuse to start.
# ---------------------------------------------------------------------------
if not settings.database_url.endswith("/taskflow_test"):
    raise RuntimeError(
        "Tests are pointed at "
        f"{settings.database_url!r}, not taskflow_test. Refusing to run: the "
        "cleanup fixture truncates every table. Check that pytest-dotenv is "
        "installed and .env.test is being loaded."
    )
if not settings.redis_url.endswith("/1"):
    raise RuntimeError(
        f"Tests are pointed at Redis {settings.redis_url!r}, not db index 1. "
        "Refusing to run: the cleanup fixture calls FLUSHDB."
    )

TEST_ENGINE = create_engine(settings.database_url)
TestSessionLocal = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture(scope="session", autouse=True)
def _load_handlers():
    """Handlers register themselves on import, and only main() calls
    load_handlers(). Without this, get_handler() returns None in tests and the
    worker tests would all "pass" for the wrong reason."""
    from worker.handlers import load_handlers

    load_handlers()


@pytest.fixture(autouse=True)
def _clean_state():
    """Before every test: wipe Postgres tables, flush the test Redis index, and
    purge the test queue. No ordering dependencies between tests."""
    with TEST_ENGINE.begin() as conn:
        conn.execute(text("TRUNCATE users, jobs, job_attempts RESTART IDENTITY CASCADE;"))
    redis_client.flushdb()
    try:
        connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
        channel = connection.channel()
        channel.queue_declare(queue=TEST_QUEUE, durable=True)
        channel.queue_purge(queue=TEST_QUEUE)
        connection.close()
    except Exception:
        pass  # a broker outage shouldn't fail tests that never touch it
    yield


@pytest.fixture
def db():
    """A session for the TEST's own setup and assertions.

    Deliberately NOT injected into the app via dependency_overrides. Sharing one
    session between the test and the request handler would make the 8-thread
    race test meaningless — SQLAlchemy sessions are not thread-safe, so eight
    threads through one session is a crash, not a race. Letting the app open its
    own per-request sessions (already pointed at the test database by .env.test)
    is what production does, and it is what puts eight real connections in
    contention on the unique constraint.
    """
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_publish(monkeypatch):
    """Stops job submission from touching RabbitMQ. Patched where the caller
    looked the name up (job_service), not where it is defined."""
    monkeypatch.setattr("app.services.job_service.publish_job", lambda job_id: None)


@pytest.fixture
def publish_to_test_queue(monkeypatch):
    """Publishes for real, but to jobs.test — never the real `jobs` queue."""
    from app.rabbitmq_client import publish_job as real_publish_job

    monkeypatch.setattr(
        "app.services.job_service.publish_job",
        lambda job_id: real_publish_job(job_id, queue_name=TEST_QUEUE),
    )


@pytest.fixture
def registered_user(client):
    email = f"{uuid.uuid4()}@example.com"
    resp = client.post("/auth/register", json={"email": email, "password": "testpass123"})
    assert resp.status_code == 201, resp.text
    return {"email": email, **resp.json()}


@pytest.fixture
def auth_headers(registered_user):
    return {"Authorization": f"Bearer {registered_user['access_token']}"}
