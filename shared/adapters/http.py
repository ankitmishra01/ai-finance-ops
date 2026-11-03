"""Stdlib HTTP for the accounting adapters: OAuth2 refresh, JSON requests, retries, idempotency keys.

No third-party HTTP client, so the adapters add no dependency. The transport is a plain callable,
so tests pass a fake that returns schema-shaped mock responses and no network is touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


class AdapterConfigError(RuntimeError):
    """A required environment variable is missing. The message names variables, never values."""


class ApiError(RuntimeError):
    def __init__(self, status: int, body, url: str) -> None:
        self.status, self.body = status, body
        # The URL may carry a realm or tenant id but never a token; bodies are platform error objects.
        super().__init__(f"HTTP {status} from {url}: {json.dumps(body)[:500] if body is not None else ''}")


@dataclass
class Response:
    status: int
    headers: dict
    body: dict | None


# (method, url, headers, body bytes or None) -> Response
Transport = Callable[[str, str, dict, bytes | None], Response]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 30) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return Response(r.status, dict(r.headers), json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"raw": raw[:500].decode(errors="replace")}
        return Response(e.code, dict(e.headers or {}), parsed)


def env(*names: str, optional: tuple[str, ...] = ()) -> dict[str, str | None]:
    """Read settings from the environment; raise AdapterConfigError listing every missing required name."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise AdapterConfigError("missing environment variable(s): " + ", ".join(missing))
    return {n: os.environ.get(n) for n in (*names, *optional)}


def idempotency_key(*parts) -> str:
    """Deterministic key from the content being written, so re-posting the same approved item after a
    timeout or crash is recognised by the platform instead of creating a duplicate."""
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class OAuth2:
    """Refresh-token grant against a token endpoint, with HTTP Basic client authentication.

    Both Intuit and Xero rotate refresh tokens: the response carries a new one and the old one
    stops working after a grace period. `on_rotate` receives the new token so the caller can write
    it back to a secret store; this class keeps it only in memory and never logs it.
    """

    token_url: str
    client_id: str
    client_secret: str
    refresh_token: str
    transport: Transport = urllib_transport
    on_rotate: Callable[[str], None] | None = None
    clock: Callable[[], float] = time.time
    access_token: str | None = field(default=None, repr=False)
    expires_at: float = 0.0

    def __repr__(self) -> str:  # keep secrets out of tracebacks and logs
        return f"OAuth2(token_url={self.token_url!r}, client_id=<set>, expires_at={self.expires_at})"

    def token(self) -> str:
        if self.access_token and self.clock() < self.expires_at - 60:
            return self.access_token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token}).encode()
        r = self.transport("POST", self.token_url, {
            "Authorization": f"Basic {basic}", "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"}, body)
        if r.status != 200 or not r.body or "access_token" not in r.body:
            # Do not echo the body: a token endpoint error can quote what was sent.
            raise ApiError(r.status, {"error": (r.body or {}).get("error")}, self.token_url)
        self.access_token = r.body["access_token"]
        self.expires_at = self.clock() + float(r.body.get("expires_in", 3600))
        new_refresh = r.body.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            if self.on_rotate:
                self.on_rotate(new_refresh)
        return self.access_token


@dataclass
class JsonClient:
    """JSON over HTTP with bearer auth and bounded retries on 429 and 5xx (honouring Retry-After)."""

    auth: OAuth2
    transport: Transport = urllib_transport
    max_retries: int = 3
    backoff_s: float = 1.0
    sleep: Callable[[float], None] = time.sleep

    def request(self, method: str, url: str, *, body: dict | None = None, headers: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.max_retries + 1):
            h = {"Authorization": f"Bearer {self.auth.token()}", "Accept": "application/json", **(headers or {})}
            if data is not None:
                h["Content-Type"] = "application/json"
            r = self.transport(method, url, h, data)
            if r.status == 401 and attempt == 0:
                self.auth.expires_at = 0  # access token revoked or expired early: refresh once
                continue
            if r.status in RETRY_STATUSES and attempt < self.max_retries:
                retry_after = {k.lower(): v for k, v in r.headers.items()}.get("retry-after")
                try:
                    wait = float(retry_after) if retry_after else self.backoff_s * 2 ** attempt
                except ValueError:
                    wait = self.backoff_s * 2 ** attempt
                self.sleep(min(wait, 60))
                continue
            if r.status >= 400:
                raise ApiError(r.status, r.body, url.split("?")[0])
            return r.body or {}
        raise ApiError(r.status, r.body, url.split("?")[0])
