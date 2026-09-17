from typing import Callable

HandlerFn = Callable[[dict], dict]


class HandlerError(Exception):
    """Input the handler itself rejected — a missing or malformed field.

    This is the PERMANENT failure signal. Retrying cannot help: the payload
    is immutable, so the same input would be rejected identically three
    times, 30 seconds apart. Jobs failing this way go straight to FAILED
    and never reach the dead-letter queue, which is reserved for jobs that
    exhausted real retries and might still succeed if re-run.

    Any OTHER exception from a handler is treated as transient and retried.
    """

_registry: dict[str, HandlerFn] = {}


def register(job_type: str):
    """Decorator: @register("csv_process") on a handler function adds it
    to the dispatch table. Importing a handler module (for its side
    effect of running this decorator) is what makes it available — see
    worker/main.py's import of csv_process."""

    def decorator(fn: HandlerFn) -> HandlerFn:
        _registry[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> HandlerFn | None:
    return _registry.get(job_type)
