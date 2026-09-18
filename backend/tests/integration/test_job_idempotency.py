import threading

BODY = {"type": "csv_process", "payload": {"csv_text": "a,b\n1,2\n"}}


def test_submission_without_a_key_always_creates_a_new_job(client, auth_headers, no_publish):
    first = client.post("/jobs", json=BODY, headers=auth_headers)
    second = client.post("/jobs", json=BODY, headers=auth_headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["job_id"] != second.json()["job_id"]


def test_idempotency_key_returns_same_job_on_replay(client, auth_headers, no_publish):
    body = {**BODY, "idempotency_key": "k1"}
    first = client.post("/jobs", json=body, headers=auth_headers)
    second = client.post("/jobs", json=body, headers=auth_headers)
    assert first.status_code == 201
    assert second.status_code == 200  # 200 means "already existed", not "created"
    assert first.json()["job_id"] == second.json()["job_id"]


def test_key_accepted_via_header_too(client, auth_headers, no_publish):
    first = client.post("/jobs", json={**BODY, "idempotency_key": "k-hdr"}, headers=auth_headers)
    second = client.post("/jobs", json=BODY, headers={**auth_headers, "Idempotency-Key": "k-hdr"})
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]


def test_conflicting_keys_in_body_and_header_rejected(client, auth_headers, no_publish):
    resp = client.post(
        "/jobs",
        json={**BODY, "idempotency_key": "one"},
        headers={**auth_headers, "Idempotency-Key": "two"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_same_key_different_body_conflicts(client, auth_headers, no_publish):
    client.post("/jobs", json={**BODY, "idempotency_key": "k2"}, headers=auth_headers)
    resp = client.post(
        "/jobs",
        json={"type": "csv_process", "payload": {"csv_text": "b\n2\n"}, "idempotency_key": "k2"},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_payload_key_order_is_not_a_different_body(client, auth_headers, no_publish):
    """JSONB has no key order, so neither should the request-equality check.
    Needs a multi-field type: csv_process has exactly one allowed key."""
    fields = {"title": "T", "body": "B", "author": "A"}
    a = client.post(
        "/jobs",
        json={"type": "pdf_generate", "payload": fields, "idempotency_key": "k-order"},
        headers=auth_headers,
    )
    reordered = {"author": "A", "body": "B", "title": "T"}
    b = client.post(
        "/jobs",
        json={"idempotency_key": "k-order", "payload": reordered, "type": "pdf_generate"},
        headers=auth_headers,
    )
    assert a.status_code == 201, a.text
    assert b.status_code == 200, b.text
    assert a.json()["job_id"] == b.json()["job_id"]


def test_keys_are_scoped_per_user(client, auth_headers, no_publish):
    other = client.post(
        "/auth/register", json={"email": "other@example.com", "password": "testpass123"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    body = {**BODY, "idempotency_key": "shared-key"}
    mine = client.post("/jobs", json=body, headers=auth_headers)
    theirs = client.post("/jobs", json=body, headers=other_headers)
    assert mine.status_code == theirs.status_code == 201
    assert mine.json()["job_id"] != theirs.json()["job_id"]


def test_concurrent_identical_requests_produce_one_job(
    client, auth_headers, db, publish_to_test_queue
):
    """The test that actually proves the design.

    Eight threads race the same key. Because no dependency override is in play,
    each request opens its own session, so eight real connections contend on the
    (user_id, idempotency_key) unique constraint — one INSERT wins, seven catch
    IntegrityError and recover the existing row. A single-threaded replay only
    ever exercises the Redis fast path and never this branch.
    """
    body = {**BODY, "idempotency_key": "race-key"}
    results = []
    lock = threading.Lock()

    def submit():
        status = client.post("/jobs", json=body, headers=auth_headers).status_code
        with lock:
            results.append(status)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1, results
    assert results.count(200) == 7, results

    from app.models.job import Job

    rows = db.query(Job).filter(Job.idempotency_key == "race-key").all()
    assert len(rows) == 1


def test_idempotency_survives_an_empty_cache(client, auth_headers, db, no_publish):
    """Redis is a fast path, not the guarantee. Wipe it and the constraint must
    still collapse a replay onto the original job."""
    from app.redis_client import redis_client

    body = {**BODY, "idempotency_key": "cache-gone"}
    first = client.post("/jobs", json=body, headers=auth_headers)
    assert first.status_code == 201

    redis_client.flushdb()

    second = client.post("/jobs", json=body, headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
