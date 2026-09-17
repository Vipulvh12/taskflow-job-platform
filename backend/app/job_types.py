"""Per-type payload contracts, shared by the API and the worker.

Phase 5 deliberately left `payload` as "any JSON object under 64KB", on the
grounds that the handler consuming a payload is what defines its shape. With
two handlers that is now knowable, so the contract lives here: the API
validates against it at submission (fail fast, 422) and the handler still
guards its own inputs (defence in depth, because a row can be edited or a
message replayed against a changed schema).

This module lives under app/ rather than worker/ so the API never has to
import worker code to know what a valid payload looks like.

Adding a job type is three edits:
  1. a member in JobType (app/core/enums.py)
  2. a payload model here, registered in JOB_PAYLOAD_SCHEMAS
  3. a handler module in worker/handlers/ — auto-discovered, no import to add
"""

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import JobType


class _PayloadBase(BaseModel):
    # forbid, not ignore: a typo'd key ("titel") should be a 422 telling the
    # client what's wrong, not a silently-dropped field that surfaces as a
    # confusing missing-required-field error.
    model_config = ConfigDict(extra="forbid")


class CsvProcessPayload(_PayloadBase):
    csv_text: str = Field(min_length=1, description="CSV content, header row first.")


class PdfGeneratePayload(_PayloadBase):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, description="Body text. Blank lines separate paragraphs.")
    author: str | None = Field(default=None, max_length=120)


JOB_PAYLOAD_SCHEMAS: dict[JobType, type[BaseModel]] = {
    JobType.CSV_PROCESS: CsvProcessPayload,
    JobType.PDF_GENERATE: PdfGeneratePayload,
}
