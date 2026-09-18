from app.redis_client import redis_client

BODY = {"type": "csv_process", "payload": {"csv_text": "a\n1\n"}}


def test_job_creation_rate_limit(client, auth_headers, no_publish):
    statuses = [
        client.post("/jobs", json=BODY, headers=auth_headers).status_code for _ in range(101)
    ]
    assert statuses.count(201) == 100
    assert statuses.count(429) == 1


def test_rate_limited_response_carries_retry_after(client, auth_headers, no_publish):
    for _ in range(100):
        client.post("/jobs", json=BODY, headers=auth_headers)
    resp = client.post("/jobs", json=BODY, headers=auth_headers)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert 0 < int(resp.headers["Retry-After"]) <= 60


def test_counter_key_always_has_a_ttl(client, auth_headers, no_publish):
    """A key left without an expiry would lock that user out permanently — the
    exact bug the INCR/EXPIRE pipeline exists to prevent."""
    client.post("/jobs", json=BODY, headers=auth_headers)
    keys = redis_client.keys("rate_limit:jobs:create:user:*")
    assert len(keys) == 1
    assert redis_client.ttl(keys[0]) > 0


def test_limits_are_per_user_not_global(client, auth_headers, no_publish):
    for _ in range(101):
        client.post("/jobs", json=BODY, headers=auth_headers)

    other = client.post(
        "/auth/register", json={"email": "unaffected@example.com", "password": "testpass123"}
    ).json()
    resp = client.post(
        "/jobs", json=BODY, headers={"Authorization": f"Bearer {other['access_token']}"}
    )
    assert resp.status_code == 201


def test_reads_are_not_rate_limited(client, auth_headers, no_publish):
    for _ in range(101):
        client.post("/jobs", json=BODY, headers=auth_headers)
    assert client.get("/jobs?page_size=1", headers=auth_headers).status_code == 200


def test_login_brute_force_limit(client):
    client.post("/auth/register", json={"email": "brute@example.com", "password": "testpass123"})
    statuses = [
        client.post(
            "/auth/login", json={"email": "brute@example.com", "password": "wrong"}
        ).status_code
        for _ in range(11)
    ]
    assert statuses.count(401) == 10
    assert statuses.count(429) == 1


def test_limiter_runs_before_the_credential_check(client):
    """This is what actually stops a brute-force loop: once the limit is hit,
    even the CORRECT password is refused rather than evaluated."""
    client.post("/auth/register", json={"email": "brute2@example.com", "password": "testpass123"})
    for _ in range(11):
        client.post("/auth/login", json={"email": "brute2@example.com", "password": "wrong"})

    resp = client.post(
        "/auth/login", json={"email": "brute2@example.com", "password": "testpass123"}
    )
    assert resp.status_code == 429


def test_register_and_login_have_separate_quotas(client):
    for i in range(11):
        client.post("/auth/register", json={"email": f"r{i}@example.com", "password": "testpass123"})
    # register's quota is exhausted; login's must be untouched
    assert client.post(
        "/auth/login", json={"email": "r0@example.com", "password": "testpass123"}
    ).status_code == 200
