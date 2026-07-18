"""A minimal fake aiohttp ClientSession for testing custom_components/farmbot/api.py.

Real aiohttp is installed in the test environment (it's a genuine runtime
dependency of api.py, same as requests/paho-mqtt), but its ClientSession
talks to real sockets. This fake reproduces just the async context-manager
surface api.py actually uses (``session.request``/``session.get``,
``resp.status``, ``resp.headers``, ``resp.content.iter_chunked``,
``resp.json``, ``resp.url.scheme``) so tests can script FarmBot's HTTP
responses without any network access.
"""
from __future__ import annotations

import json as json_module


class FakeAsyncIterator:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class FakeContent:
    def __init__(self, body: bytes):
        self._body = body

    def iter_chunked(self, chunk_size):
        if not self._body:
            return FakeAsyncIterator([])
        chunks = [self._body[i:i + chunk_size] for i in range(0, len(self._body), chunk_size)]
        return FakeAsyncIterator(chunks)


class FakeURL:
    def __init__(self, scheme="https"):
        self.scheme = scheme


class FakeResponse:
    """A scripted aiohttp response, usable as an async context manager."""

    def __init__(
        self,
        *,
        status: int = 200,
        json_body=None,
        body: bytes | None = None,
        headers: dict | None = None,
        url_scheme: str = "https",
        content_type: str = "application/json",
    ):
        self.status = status
        if body is not None:
            self._body = body
        elif json_body is not None:
            self._body = json_module.dumps(json_body).encode()
        else:
            self._body = b""
        self.headers = dict(headers or {})
        if content_type and "Content-Type" not in self.headers:
            self.headers["Content-Type"] = content_type
        self.content = FakeContent(self._body)
        self.url = FakeURL(url_scheme)
        self._json_body = json_body

    async def json(self, content_type=None):
        if self._json_body is None:
            raise ValueError("no json body scripted for this FakeResponse")
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    """Returns scripted FakeResponses in order and records every call made."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError(f"FakeSession had no scripted response left for {method} {url}")
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if not self._responses:
            raise AssertionError(f"FakeSession had no scripted response left for GET {url}")
        return self._responses.pop(0)
