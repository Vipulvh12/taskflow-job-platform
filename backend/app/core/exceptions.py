class AppError(Exception):
    """Base class for errors we raise on purpose from business logic.

    Each subclass maps to one HTTP status + a stable machine-readable code
    the frontend can branch on without parsing message text.
    """

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class BadRequestError(AppError):
    """For requests that are well-formed enough for Pydantic to accept but
    are still invalid as a whole — e.g. two conflicting idempotency keys in
    the same request. Pydantic validates fields; this covers the rest."""

    status_code = 400
    code = "bad_request"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int):
        self.retry_after = retry_after
        super().__init__(message)


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class QueuePublishError(AppError):
    status_code = 502
    code = "queue_unavailable"
