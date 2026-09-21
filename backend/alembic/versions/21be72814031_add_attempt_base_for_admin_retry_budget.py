"""add attempt_base for admin retry budget

Revision ID: 21be72814031
Revises: 252ae359c5a3
Create Date: 2026-09-21 18:53:47.939269

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '21be72814031'
down_revision: Union[str, Sequence[str], None] = '252ae359c5a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """attempt_count at the moment of the last manual retry.

    An admin retrying a DEAD job is granting it a fresh retry budget. Resetting
    attempt_count to 0 would do that, but the next attempt would then be numbered
    1 again — job_attempts has no uniqueness on (job_id, attempt_number) — and
    the history would read 1, 2, 3, 1, 2, 3. Instead attempt_count keeps counting
    over the job's whole life, and the budget is measured from this baseline:
    attempts 4, 5, 6 are the new ladder, and the history stays readable.

    NOT NULL with a constant default is a metadata-only change on Postgres 11+,
    so it does not rewrite the 200K-row table.
    """
    op.add_column(
        "jobs",
        sa.Column("attempt_base", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("jobs", "attempt_base")
