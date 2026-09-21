"""Load test against the running Docker stack. From backend/:

    .venv/Scripts/locust -f loadtest/locustfile.py --host http://127.0.0.1:8000

127.0.0.1 rather than localhost: on this machine localhost costs ~10 ms more
per new connection (it tries IPv6 first), which would land in the latency tail.

Who the simulated users are
---------------------------
Reads go to the bench-NN accounts, which own the ~200K seeded rows (~10K
each). That is the point: the Phase 14b index only matters for a user with a
large history — a fresh account's list query returns nothing, fast, with or
without it. Writes go to the loadtest-NN accounts (seed_loadtest_users.py), so
the Phase 14 dataset itself isn't modified and cleanup is one LIKE pattern.

How they authenticate
---------------------
Access tokens are minted at test start with the app's own create_access_token
— the same claims /auth/login issues, verified by the same code path on every
request. They can't come from /auth/login:
  - it is rate-limited to 10/min per IP, and every Locust user shares one IP;
  - *@taskflow.local can't log in over HTTP at all: .local is a special-use
    domain (RFC 6762) and EmailStr rejects it with a 422.
So login latency is not part of what this test measures.
"""
import itertools
import random
import sys
from pathlib import Path

# Locust puts this file's directory on sys.path, not backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loadtest import host_env  # noqa: E402,F401  (must precede any app import)

from locust import FastHttpUser, between, events, task  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.security import create_access_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402

FILTER_STATUSES = ["QUEUED", "SUCCESS", "DEAD"]
KNOWN_IDS_PER_USER = 50

_reader_tokens: list[str] = []
_writer_tokens: list[str] = []
_user_counter = itertools.count()


def _tokens_for(pattern: str) -> list[str]:
    db = SessionLocal()
    try:
        ids = db.execute(
            select(User.id).where(User.email.like(pattern)).order_by(User.email)
        ).scalars().all()
    finally:
        db.close()
    return [create_access_token(str(i), is_admin=False) for i in ids]


@events.test_start.add_listener
def mint_tokens(environment, **_kwargs):
    # Minted per run: access tokens live 15 minutes, longer than any run here.
    _reader_tokens[:] = _tokens_for("bench-%@taskflow.local")
    _writer_tokens[:] = _tokens_for("loadtest-%@taskflow.local")
    if not _reader_tokens or not _writer_tokens:
        raise SystemExit(
            "Missing accounts: bench-NN comes from scripts/seed_jobs.py, "
            "loadtest-NN from loadtest/seed_loadtest_users.py."
        )
    print(f"Minted tokens: {len(_reader_tokens)} readers, {len(_writer_tokens)} writers.")


class JobPlatformUser(FastHttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Round-robin, so identities are spread evenly rather than randomly
        # clumped: 1,000 users → 50 per bench account, 20 per loadtest account.
        n = next(_user_counter)
        self.read_headers = {"Authorization": f"Bearer {_reader_tokens[n % len(_reader_tokens)]}"}
        self.write_headers = {"Authorization": f"Bearer {_writer_tokens[n % len(_writer_tokens)]}"}
        self.known_ids: list[str] = []

    @task(10)
    def list_jobs(self):
        # The query Phase 14b's (user_id, created_at DESC, id DESC) index exists
        # for — plus the COUNT(*) that runs alongside it for the page total.
        with self.client.get(
            "/jobs?page=1&page_size=20",
            headers=self.read_headers,
            name="/jobs [list]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                ids = [item["id"] for item in resp.json()["items"]]
                self.known_ids = (ids + self.known_ids)[:KNOWN_IDS_PER_USER]

    @task(5)
    def list_jobs_filtered(self):
        status = random.choice(FILTER_STATUSES)
        self.client.get(
            f"/jobs?status={status}&page=1&page_size=20",
            headers=self.read_headers,
            name="/jobs [filtered]",
        )

    @task(3)
    def get_job(self):
        # A real single-job fetch (primary-key lookup + its attempts), using ids
        # this user has seen in its own list responses.
        if not self.known_ids:
            return self.list_jobs()
        self.client.get(
            f"/jobs/{random.choice(self.known_ids)}",
            headers=self.read_headers,
            name="/jobs/{id}",
        )

    @task(1)
    def submit_job(self):
        # Low weight on purpose: this measures submission latency under read
        # load, not the worker fleet's capacity. The payload is valid, so these
        # jobs succeed rather than landing in the admin dead-letter view.
        self.client.post(
            "/jobs",
            json={"type": "csv_process", "payload": {"csv_text": "a,b\n1,2\n"}},
            headers=self.write_headers,
            name="/jobs [submit]",
        )
