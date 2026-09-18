import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.core.exceptions import AppError, RateLimitError
from app.routers import auth, health, jobs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow")

# Where in the request an error occurred; useful to FastAPI, noise to clients.
_LOC_SECTIONS = {"body", "query", "path", "header", "cookie"}

app = FastAPI(title="TaskFlow API")

# The Vite dev server is a different origin (port 5173) from this API (8000),
# so without this every browser request fails preflight before reaching a route.
# No allow_credentials: that flag is for cookie-based auth, and tokens travel in
# the Authorization header (Phase 4), so there are no credentialed requests to
# permit. Origins are listed explicitly — "*" plus credentials is rejected by
# browsers anyway, and a wildcard here would be a needless invitation.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(jobs.router)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimitError) else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # exc.errors() is Pydantic's detailed error list; we fold the first
    # error into one readable message rather than exposing Pydantic's
    # internal error structure to API clients.
    first = exc.errors()[0]
    if first.get("type") == "json_invalid":
        # loc here is ("body", <byte offset>) — an integer, not a field. Folding
        # it in the usual way produces nonsense like "1: JSON decode error".
        message = "Malformed JSON body."
    else:
        # Drop the request-part marker ("body"/"query"/"path"/"header") and any
        # non-string element (list indices) so what's left reads as a field path.
        parts = [p for p in first["loc"] if isinstance(p, str) and p not in _LOC_SECTIONS]
        field = ".".join(parts)
        # Pydantic prefixes any ValueError raised inside a custom validator with
        # "Value error, ". That's an internal detail; clients get the message.
        msg = first["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        message = f"{field}: {msg}" if field else msg
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": message}},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Catches FastAPI's own raised HTTPExceptions (e.g. from auth helpers
    # in Phase 4) AND unmatched routes (plain 404s), so every error the
    # API can produce goes through the same envelope.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": exc.detail}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Something went wrong."}},
    )
