from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states. QUEUED -> RUNNING -> SUCCESS is the happy path;
    a failure goes RUNNING -> RETRYING -> RUNNING ... -> DEAD once retries
    are exhausted (Phase 8). FAILED is a terminal failure that is not
    retryable."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DEAD = "DEAD"


class JobType(StrEnum):
    """Job types the platform accepts. A type is only listed here once the
    API is willing to take it — the worker's handler for it lands in
    Phase 7 (csv_process) and Phase 9 (pdf_generate)."""

    CSV_PROCESS = "csv_process"
    PDF_GENERATE = "pdf_generate"
