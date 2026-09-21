"""Global request-body limit (app/body_limit.py).

The raw-ASGI tests at the bottom are the ones that prove the memory problem is
gone: they drive the app with a receive() that counts every body byte the app
pulls, so "rejected" can't hide "read all 30 MB, then rejected".
"""
import asyncio
import json

from app.config import settings
from app.main import app
from app.redis_client import redis_client
from app.schemas.job import MAX_PAYLOAD_BYTES

LIMIT = settings.max_request_body_bytes
THIRTY_MB = 30 * 1024 * 1024
CHUNK = 64 * 1024
JSON = {"Content-Type": "application/json"}


def assert_too_large(status, body):
    assert status == 413
    assert json.loads(body) == {
        "error": {
            "code": "payload_too_large",
            "message": f"Request body must be at most {LIMIT} bytes.",
        }
    }


# ------------------------------------------------------- through the client ---


def test_largest_legitimate_job_submission_is_accepted(client, auth_headers, no_publish):
    # A payload exactly at the 64 KiB cap as the validator measures it,
    # pretty-printed, with a maximum-length idempotency key: the biggest body a
    # real client sends. It must fit under the limit and succeed.
    csv_text = "a" * (MAX_PAYLOAD_BYTES - len(json.dumps({"csv_text": ""})))
    assert len(json.dumps({"csv_text": csv_text}).encode()) == MAX_PAYLOAD_BYTES
    body = json.dumps(
        {"type": "csv_process", "payload": {"csv_text": csv_text}, "idempotency_key": "k" * 255},
        indent=2,
    ).encode()
    assert MAX_PAYLOAD_BYTES < len(body) < LIMIT

    resp = client.post("/jobs", content=body, headers={**auth_headers, **JSON})
    assert resp.status_code == 201, resp.text


def test_body_of_exactly_the_limit_is_read(client):
    # Not rejected by size: it reaches the JSON parser, which rejects the junk.
    resp = client.post("/auth/login", content=b"x" * LIMIT, headers=JSON)
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "Malformed JSON body."


def test_one_byte_over_the_limit_is_rejected(client):
    resp = client.post("/auth/login", content=b"x" * (LIMIT + 1), headers=JSON)
    assert_too_large(resp.status_code, resp.content)
    assert resp.headers["content-type"] == "application/json"


def test_oversized_body_on_an_endpoint_that_never_reads_it_is_still_rejected(client):
    # GET /health ignores its body. The middleware replaces its response
    # instead — the other of the two paths Starlette's middleware rejects by.
    resp = client.request("GET", "/health", content=b"x" * (LIMIT + 1))
    assert_too_large(resp.status_code, resp.content)


def test_30mb_login_is_rejected_before_login_logic_runs(client):
    body = json.dumps({"email": "x" * THIRTY_MB + "@example.com", "password": "p"}).encode()
    resp = client.post("/auth/login", content=body, headers=JSON)
    assert_too_large(resp.status_code, resp.content)
    # Before the limit this was a 422 from email validation, after a full parse.
    # Not even the login rate limiter (a dependency) ran.
    assert not redis_client.keys("rate_limit:auth:login:*")


def test_normal_login_still_works(client, registered_user):
    resp = client.post(
        "/auth/login",
        json={"email": registered_user["email"], "password": "testpass123"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_normal_authenticated_requests_still_work(client, auth_headers, no_publish):
    submitted = client.post(
        "/jobs",
        json={"type": "csv_process", "payload": {"csv_text": "a,b\n1,2\n"}},
        headers=auth_headers,
    )
    assert submitted.status_code == 201
    listed = client.get("/jobs", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


# ---------------------------------------------------- raw ASGI: bytes pulled ---


def call_app(headers, total_bytes):
    """Drives the ASGI app directly with a POST /auth/login whose body is
    `total_bytes` of JSON-ish filler, streamed in 64 KiB chunks. Returns the
    status, the response body, and how many body bytes the app pulled."""
    pulled = 0
    remaining = total_bytes
    chunk = b"x" * CHUNK
    sent = []

    async def receive():
        nonlocal pulled, remaining
        if remaining > 0:
            size = min(CHUNK, remaining)
            remaining -= size
            pulled += size
            return {"type": "http.request", "body": chunk[:size], "more_body": remaining > 0}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/login",
        "raw_path": b"/auth/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, pulled


def test_30mb_login_with_content_length_is_rejected_without_reading_a_byte():
    status, body, pulled = call_app([(b"content-length", str(THIRTY_MB).encode())], THIRTY_MB)
    assert_too_large(status, body)
    assert pulled == 0


def test_30mb_chunked_login_stops_reading_at_the_limit():
    # No Content-Length to check up front: the limit is enforced while
    # streaming, so the app reads at most one chunk past it.
    status, body, pulled = call_app([(b"transfer-encoding", b"chunked")], THIRTY_MB)
    assert_too_large(status, body)
    assert LIMIT < pulled <= LIMIT + CHUNK
