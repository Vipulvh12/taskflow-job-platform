import uuid

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, RateLimitError, UnauthorizedError
from app.core.security import decode_token
from app.db import get_db
from app.models.user import User
from app.redis_client import redis_client


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if authorization is None or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Missing or malformed Authorization header.")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Access token has expired.")
    except jwt.PyJWTError:
        raise UnauthorizedError("Invalid access token.")

    if payload.get("type") != "access":
        raise UnauthorizedError("Token is not an access token.")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise UnauthorizedError("Malformed token subject.")

    user = db.get(User, user_id)
    if user is None:
        raise UnauthorizedError("User no longer exists.")

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise ForbiddenError("Admin privileges required.")
    return user


def _check_rate_limit(key: str, max_requests: int, window_seconds: int) -> None:
    """Fixed-window counter. INCR and EXPIRE go out in one pipeline rather
    than as `if INCR == 1: EXPIRE`, because a crash between those two calls
    would leave the key incremented with no TTL — locking that caller out
    permanently, fixable only by deleting the key by hand. EXPIRE ... NX
    sets the TTL only when the key has none, so concurrent requests inside
    a window can't keep pushing the expiry outward (which would turn the
    fixed window into an indefinite one)."""
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds, nx=True)
    current, _ = pipe.execute()

    if current > max_requests:
        ttl = redis_client.ttl(key)
        retry_after = ttl if ttl and ttl > 0 else window_seconds
        raise RateLimitError(
            f"Rate limit exceeded: max {max_requests} requests per {window_seconds}s.",
            retry_after=retry_after,
        )


def rate_limit_by_user(scope: str, max_requests: int, window_seconds: int):
    """Keys the limit to the authenticated user — for endpoints behind auth."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        _check_rate_limit(f"rate_limit:{scope}:user:{user.id}", max_requests, window_seconds)
        return user

    return dependency


def rate_limit_by_ip(scope: str, max_requests: int, window_seconds: int):
    """Keys the limit to the client IP — for pre-auth endpoints like login,
    where no user identity exists yet (that's the whole point of the attack).

    request.client.host is the direct TCP peer, correct while Uvicorn is
    exposed directly. Behind a reverse proxy or load balancer every request
    would appear to come from the proxy, and this would need to read a
    trusted X-Forwarded-For instead."""

    def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        _check_rate_limit(f"rate_limit:{scope}:ip:{client_ip}", max_requests, window_seconds)

    return dependency
