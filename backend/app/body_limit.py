"""Global request-body limit: Starlette's RequestBodyLimitMiddleware, with its
413 rendered in the API's error envelope.

Why: a 30 MB unauthenticated POST /auth/login used to be read, JSON-parsed and
validated in full before being rejected — 1.6 s and +57 MB of API memory per
request (Phase 17 review). FastAPI reads the body before any dependency runs,
so the login rate limiter never got a say.

How Starlette's middleware rejects (it does not short-circuit):
  - Content-Length over the limit: the app's first receive() raises before a
    single body byte is read, and if the app responds without reading the
    body, that response is replaced with a 413.
  - No Content-Length (chunked): bytes are counted as they arrive, and it
    stops at the limit plus one chunk.

Its 413 is plain text ("Content Too Large"). Every other error this API
returns is {"error": {"code", "message"}}, and the frontend reads error.message
from that shape — so the wrapper below swaps the body of any 413 for the
envelope. Enforcement is entirely the library's.
"""
import json

from starlette.middleware.body_limit import RequestBodyLimitMiddleware


def too_large_body(max_body_size: int) -> bytes:
    return json.dumps(
        {
            "error": {
                "code": "payload_too_large",
                "message": f"Request body must be at most {max_body_size} bytes.",
            }
        }
    ).encode("utf-8")


class BodyLimitMiddleware:
    def __init__(self, app, max_body_size: int):
        self.app = RequestBodyLimitMiddleware(app, max_body_size=max_body_size)
        self.body = too_large_body(max_body_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        replacing = False

        async def send_enveloped(message):
            nonlocal replacing
            if message["type"] == "http.response.start" and message["status"] == 413:
                replacing = True
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(self.body)).encode()),
                    ],
                })
            elif replacing:
                # Drop the original body; send ours once it has finished.
                if message["type"] == "http.response.body" and not message.get("more_body"):
                    await send({"type": "http.response.body", "body": self.body})
            else:
                await send(message)

        await self.app(scope, receive, send_enveloped)
