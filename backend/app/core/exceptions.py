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


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"
