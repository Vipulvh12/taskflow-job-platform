"""The one way a job goes back into the queue.

Two callers need it — the reaper (RUNNING -> QUEUED, for an abandoned job) and
an admin retry (DEAD -> QUEUED) — and both need exactly the same three steps:

  1. a CONDITIONAL status transition, so that of two racing callers exactly one
     wins and the other learns it lost;
  2. a publish, because a QUEUED row with no message is invisible to workers;
  3. if that publish fails, putting the row back exactly as it was, so the job is
     neither stranded at QUEUED with no message nor lost to the caller.

Keeping one implementation is the point. Phase 15 showed what happens when a
second, uncoordinated path exists for the same state transition.

Lives in app/ rather than worker/ because the API calls it, and the API never
imports worker code. The reaper imports it from here.
"""

import enum
import logging
import uuid
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.models.job import Job
from app.rabbitmq_client import publish_job

logger = logging.getLogger("taskflow")


class RequeueOutcome(enum.Enum):
    REQUEUED = "requeued"
    NOT_IN_EXPECTED_STATE = "not_in_expected_state"
    PUBLISH_FAILED = "publish_failed"


def requeue(
    db: Session,
    job_id: uuid.UUID,
    *,
    expected_status: str,
    set_values: dict[str, Any] | None = None,
    restore_values: dict[str, Any] | None = None,
    publish: Callable[[str], None] | None = None,
) -> RequeueOutcome:
    """Move a job from `expected_status` to QUEUED and publish it.

    `set_values` are applied alongside the status change; `restore_values` are
    what to put back if the publish fails, so a caller that changed other fields
    (an admin retry resetting the budget) gets its row restored completely.
    """
    # Resolved at call time rather than as a default argument, so tests can
    # monkeypatch the module-level publish_job.
    publish = publish if publish is not None else publish_job

    won = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == expected_status)
        .values(status=JobStatus.QUEUED.value, **(set_values or {}))
        .returning(Job.id)
    ).first()
    db.commit()
    if won is None:
        return RequeueOutcome.NOT_IN_EXPECTED_STATE

    try:
        publish(str(job_id))
    except Exception:  # noqa: BLE001 — any publish failure must be undone
        # Phase 6's dual-write problem. Conditional on still being QUEUED, in case
        # something else has already moved it on.
        db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.QUEUED.value)
            .values(status=expected_status, **(restore_values or {}))
        )
        db.commit()
        logger.exception("Requeue of job %s: publish failed, restored to %s.",
                         job_id, expected_status)
        return RequeueOutcome.PUBLISH_FAILED

    return RequeueOutcome.REQUEUED
