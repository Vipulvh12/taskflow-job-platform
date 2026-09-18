import jwt
import pytest

from app.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    hashed = hash_password("correct-password")
    assert verify_password("correct-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_hash_is_not_plaintext():
    assert hash_password("mypassword") != "mypassword"


def test_same_password_hashes_differently_each_time():
    # bcrypt salts per call; identical hashes would mean the salt isn't random.
    assert hash_password("same") != hash_password("same")


def test_access_token_roundtrip():
    token = create_access_token("some-user-id", is_admin=False)
    payload = decode_token(token)
    assert payload["sub"] == "some-user-id"
    assert payload["type"] == "access"
    assert payload["is_admin"] is False


def test_refresh_token_has_jti():
    token, jti = create_refresh_token("some-user-id")
    payload = decode_token(token)
    assert payload["jti"] == jti
    assert payload["type"] == "refresh"


def test_each_refresh_token_gets_a_distinct_jti():
    _, first = create_refresh_token("u")
    _, second = create_refresh_token("u")
    assert first != second


def test_expired_token_is_rejected():
    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    expired = jwt.encode(
        {"sub": "u", "type": "access", "exp": past},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(expired)


def test_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode({"sub": "u", "type": "access"}, "not-the-real-secret", algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(forged)
