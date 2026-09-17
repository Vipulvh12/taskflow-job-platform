"""Handler package.

Handlers register themselves via the @register decorator, which only runs when
the module is imported. Relying on an explicit import list in worker/main.py
meant a new handler file that nobody remembered to import would silently never
register — and the symptom is a job failing with "no handler registered", which
looks like a deployment problem rather than a missing import.

load_handlers() imports every module in this package instead, so dropping a
file in here is all it takes.
"""

import importlib
import pkgutil

_SKIP = {"registry"}


def load_handlers() -> None:
    for module in pkgutil.iter_modules(__path__):
        if module.name in _SKIP or module.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module.name}")
