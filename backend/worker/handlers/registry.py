from typing import Callable

HandlerFn = Callable[[dict], dict]

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
