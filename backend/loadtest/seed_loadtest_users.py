"""Creates the dedicated accounts the load test *writes* as. Run once, from
backend/, on the host:

    .venv/Scripts/python -m loadtest.seed_loadtest_users

Writes directly through the ORM rather than POST /auth/register, which is
limited to 10/min per IP: 50 accounts would take five minutes of sleeps, or a
temporarily raised limit that someone has to remember to lower again. Same
approach as scripts/seed_jobs.py for the bench-NN accounts.

Never touches real accounts — only emails matching loadtest-NN@taskflow.local.
Cleanup (jobs first; the foreign key has no ON DELETE CASCADE):

    DELETE FROM job_attempts WHERE job_id IN (SELECT id FROM jobs WHERE user_id IN
        (SELECT id FROM users WHERE email LIKE 'loadtest-%@taskflow.local'));
    DELETE FROM jobs WHERE user_id IN
        (SELECT id FROM users WHERE email LIKE 'loadtest-%@taskflow.local');
    DELETE FROM users WHERE email LIKE 'loadtest-%@taskflow.local';
"""
from loadtest import host_env  # noqa: F401  (must precede any app import)

import uuid

from sqlalchemy import select

from app.core.security import hash_password
from app.db import SessionLocal
from app.models.user import User

NUM_USERS = 50
PASSWORD = "loadtest123"


def main():
    # One hash for all 50: bcrypt is deliberately slow (~0.25s), and these are
    # throwaway accounts sharing one password anyway.
    pw = hash_password(PASSWORD)
    db = SessionLocal()
    try:
        created = 0
        for i in range(NUM_USERS):
            email = f"loadtest-{i:02d}@taskflow.local"
            exists = db.execute(select(User.id).where(User.email == email)).first()
            if exists is None:
                db.add(User(id=uuid.uuid4(), email=email, password_hash=pw))
                created += 1
        db.commit()
        print(f"Ready: {NUM_USERS} loadtest-NN@taskflow.local accounts ({created} new).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
