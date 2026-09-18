def test_register_then_me(client):
    resp = client.post("/auth/register", json={"email": "a@example.com", "password": "testpass123"})
    assert resp.status_code == 201
    tokens = resp.json()

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "a@example.com"
    assert me.json()["is_admin"] is False


def test_duplicate_register_conflicts(client):
    client.post("/auth/register", json={"email": "dup@example.com", "password": "testpass123"})
    resp = client.post("/auth/register", json={"email": "dup@example.com", "password": "testpass123"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


def test_login_wrong_password_401(client):
    client.post("/auth/register", json={"email": "b@example.com", "password": "testpass123"})
    resp = client.post("/auth/login", json={"email": "b@example.com", "password": "wrongpass"})
    assert resp.status_code == 401


def test_unknown_email_and_wrong_password_are_indistinguishable(client):
    """Different messages here would let an attacker enumerate accounts."""
    client.post("/auth/register", json={"email": "known@example.com", "password": "testpass123"})
    wrong_pw = client.post("/auth/login", json={"email": "known@example.com", "password": "nope"})
    no_user = client.post("/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


def test_me_requires_a_bearer_token(client, registered_user):
    assert client.get("/auth/me").status_code == 401
    # raw token without the scheme
    assert client.get(
        "/auth/me", headers={"Authorization": registered_user["access_token"]}
    ).status_code == 401


def test_refresh_token_is_rejected_as_an_access_token(client, registered_user):
    resp = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {registered_user['refresh_token']}"}
    )
    assert resp.status_code == 401
    assert "not an access token" in resp.json()["error"]["message"]


def test_access_token_is_rejected_as_a_refresh_token(client, registered_user):
    resp = client.post("/auth/refresh", json={"refresh_token": registered_user["access_token"]})
    assert resp.status_code == 401
    assert "not a refresh token" in resp.json()["error"]["message"]


def test_refresh_rotates_and_old_token_dies(client, registered_user):
    resp = client.post("/auth/refresh", json={"refresh_token": registered_user["refresh_token"]})
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] != registered_user["refresh_token"]

    replay = client.post("/auth/refresh", json={"refresh_token": registered_user["refresh_token"]})
    assert replay.status_code == 401


def test_logout_revokes_refresh_token(client, registered_user):
    logout = client.post("/auth/logout", json={"refresh_token": registered_user["refresh_token"]})
    assert logout.status_code == 204
    assert logout.content == b""

    resp = client.post("/auth/refresh", json={"refresh_token": registered_user["refresh_token"]})
    assert resp.status_code == 401


def test_logout_with_garbage_is_idempotent(client):
    assert client.post("/auth/logout", json={"refresh_token": "not-a-jwt"}).status_code == 204


def test_password_byte_limit_is_bytes_not_characters(client):
    """bcrypt caps input at 72 BYTES. 40 multi-byte characters is 80 bytes."""
    resp = client.post("/auth/register", json={"email": "u@example.com", "password": "é" * 40})
    assert resp.status_code == 422
    assert "72 bytes" in resp.json()["error"]["message"]


def test_short_password_rejected(client):
    resp = client.post("/auth/register", json={"email": "u2@example.com", "password": "abc"})
    assert resp.status_code == 422


def test_error_envelope_shape_is_consistent(client):
    """Every error the API produces uses one shape, so the frontend needs one
    parsing path."""
    for resp in [
        client.get("/auth/me"),
        client.get("/nonexistent-route"),
        client.post("/auth/register", json={"email": "bad", "password": "testpass123"}),
    ]:
        body = resp.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message"}
