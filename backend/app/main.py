import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError
from app.routers import auth, health

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow")

app = FastAPI(title="TaskFlow API")

app.include_router(health.router)
app.include_router(auth.router)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # exc.errors() is Pydantic's detailed error list; we fold the first
    # error into one readable message rather than exposing Pydantic's
    # internal error structure to API clients.
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first["loc"] if p != "body")
    message = f"{field}: {first['msg']}" if field else first["msg"]
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
