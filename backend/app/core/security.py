import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: str, is_admin: bool) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "is_admin": is_admin,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> tuple[str, str]:
    """Returns (token, jti). Caller is responsible for recording the jti
    in Redis — this function only builds the token, it doesn't know about
    storage."""
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expire_days),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti


def decode_token(token: str) -> dict:
    """Raises jwt.ExpiredSignatureError or another jwt.PyJWTError subclass
    on invalid/expired tokens — callers must catch these explicitly."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
