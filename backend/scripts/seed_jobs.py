"""One-off seed script for Phase 14 benchmarking. NOT part of the app — run
manually, never imported by API or worker code.

    docker cp backend/scripts/seed_jobs.py taskflow-api-1:/app/scripts/seed_jobs.py
    docker exec taskflow-api-1 python -m scripts.seed_jobs

Seeded rows belong to dedicated `bench-N@taskflow.local` users rather than real
accounts. Seeded jobs in QUEUED/RUNNING/RETRYING have no queue message behind
them, so they never settle — attached to a real account, they would keep that
account's job list polling every 2 seconds forever. Isolating them also makes
cleanup two statements (jobs first — the foreign key has no ON DELETE CASCADE):

    DELETE FROM jobs WHERE user_id IN
        (SELECT id FROM users WHERE email LIKE 'bench-%@taskflow.local');
    DELETE FROM users WHERE email LIKE 'bench-%@taskflow.local';
"""
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, insert, select

from app.core.security import hash_password
from app.db import SessionLocal
from app.models.job import Job
from app.models.user import User

STATUSES = ["QUEUED", "RUNNING", "SUCCESS", "FAILED", "RETRYING", "DEAD"]
TYPES = ["csv_process", "pdf_generate"]
BENCH_USERS = 20
TARGET_ROWS = 200_000
BATCH_SIZE = 5_000
SEED = 14  # deterministic, so a rerun produces the same distribution


def bench_user_ids(db) -> list[uuid.UUID]:
    """User cardinality is what matters for idx_jobs_user_status, not which
    users they are. 20 users puts ~5% of the table under each."""
    pw = hash_password("not-a-real-login-benchmark-only")
    ids = []
    for i in range(BENCH_USERS):
        email = f"bench-{i:02d}@taskflow.local"
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(id=uuid.uuid4(), email=email, password_hash=pw)
            db.add(user)
            db.flush()
        ids.append(user.id)
    db.commit()
    return ids


def main():
    random.seed(SEED)
    db = SessionLocal()
    try:
        user_ids = bench_user_ids(db)
        existing = db.execute(select(func.count()).select_from(Job)).scalar_one()
        print(f"Starting from {existing} existing rows, seeding toward {TARGET_ROWS}.")

        now = datetime.now(timezone.utc)
        inserted = 0
        started = time.perf_counter()
        while existing + inserted < TARGET_ROWS:
            n = min(BATCH_SIZE, TARGET_ROWS - existing - inserted)
            batch = [
                {
                    "id": uuid.uuid4(),
                    "user_id": random.choice(user_ids),
                    "type": random.choice(TYPES),
                    "status": random.choice(STATUSES),
                    "priority": random.randint(0, 10),
                    "payload": {"seeded": True},
                    "attempt_count": random.randint(0, 3),
                    "created_at": now - timedelta(seconds=random.randint(0, 180 * 86400)),
                }
                for _ in range(n)
            ]
            # Core insert() rather than text(): SQLAlchemy batches it into
            # multi-row VALUES statements, and handles the UUID and JSONB types
            # itself — no manual CAST(:payload AS jsonb).
            db.execute(insert(Job.__table__), batch)
            db.commit()
            inserted += n
            print(f"  inserted {inserted:>7,} rows  ({time.perf_counter() - started:5.1f}s)")

        print(f"Done. Total rows now: {existing + inserted:,}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
