import uuid

import jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.redis_client import redis_client

REFRESH_KEY_PREFIX = "refresh_token:"


def _store_refresh_jti(jti: str, user_id: str) -> None:
    redis_client.setex(
        f"{REFRESH_KEY_PREFIX}{jti}",
        settings.refresh_token_expire_days * 24 * 60 * 60,
        user_id,
    )


def _revoke_refresh_jti(jti: str) -> None:
    redis_client.delete(f"{REFRESH_KEY_PREFIX}{jti}")


def _issue_token_pair(user: User) -> tuple[str, str]:
    access_token = create_access_token(str(user.id), user.is_admin)
    refresh_token, jti = create_refresh_token(str(user.id))
    _store_refresh_jti(jti, str(user.id))
    return access_token, refresh_token


def register(db: Session, email: str, password: str) -> tuple[str, str]:
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Same race the idempotency unique constraint guards against in
        # Phase 5's design: two registers for the same email racing each
        # other. The DB constraint is the real guarantee; this just turns
        # it into a clean 409 instead of a raw IntegrityError leaking out.
        db.rollback()
        raise ConflictError("An account with this email already exists.")
    db.refresh(user)
    return _issue_token_pair(user)


def login(db: Session, email: str, password: str) -> tuple[str, str]:
    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(password, user.password_hash):
        # Deliberately the same error for "no such user" and "wrong
        # password" — distinguishing them lets an attacker enumerate
        # which emails have accounts.
        raise UnauthorizedError("Invalid email or password.")
    return _issue_token_pair(user)


def refresh(db: Session, refresh_token: str) -> tuple[str, str]:
    try:
        payload = decode_token(refresh_token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Refresh token has expired.")
    except jwt.PyJWTError:
        raise UnauthorizedError("Invalid refresh token.")

    if payload.get("type") != "refresh":
        raise UnauthorizedError("Token is not a refresh token.")

    jti = payload["jti"]
    stored_user_id = redis_client.get(f"{REFRESH_KEY_PREFIX}{jti}")
    if stored_user_id is None:
        raise UnauthorizedError("Refresh token has been revoked or expired.")

    _revoke_refresh_jti(jti)  # rotation: this refresh token is now dead

    user = db.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise UnauthorizedError("User no longer exists.")

    return _issue_token_pair(user)


def logout(refresh_token: str) -> None:
    try:
        payload = decode_token(refresh_token)
    except jwt.PyJWTError:
        return  # already invalid/expired — nothing to revoke, not an error
    if payload.get("type") == "refresh":
        _revoke_refresh_jti(payload["jti"])
