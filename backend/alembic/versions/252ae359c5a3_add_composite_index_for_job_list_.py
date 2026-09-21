"""add composite index for job list ordering

Revision ID: 252ae359c5a3
Revises: c034523a261d
Create Date: 2026-09-21 18:09:00.586733

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '252ae359c5a3'
down_revision: Union[str, Sequence[str], None] = 'c034523a261d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Index shaped like GET /jobs's ORDER BY, measured in Phase 14.

    Neither Phase 2 index can deliver `ORDER BY created_at DESC, id DESC`, so the
    default job list — polled every 2s by the frontend — fetched every one of a
    user's rows and sorted them to return 20. With this index Postgres reads the
    first 20 entries in order and stops: 9.05 ms -> 0.050 ms for a 10K-job user.

    `id DESC` is not decorative. created_at can tie under concurrent inserts, and
    without a deterministic tiebreaker offset pagination may show a row on two
    pages or skip it; the query already orders by it, so the index must too.

    Not CONCURRENTLY: that cannot run inside Alembic's transaction and this table
    is small. On a large production table, create it CONCURRENTLY out of band.
    """
    op.create_index(
        "idx_jobs_user_created",
        "jobs",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_jobs_user_created", table_name="jobs")
