import uuid

import jwt
from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.db import get_db
from app.models.user import User


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
