"""Tiny JSON-over-HTTP transport shared by the Habitica and Notion clients (standard library only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class TransportError(Exception):
    """Network-level failure (DNS, timeout, connection reset)."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: Mapping[str, str],
                 body: dict | None, timeout: float) -> Response: ...


def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: dict | None, timeout: float = 30.0) -> Response:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = dict(headers)
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status, resp_headers = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, resp_headers = exc.read(), exc.code, exc.headers
    except urllib.error.URLError as exc:
        raise TransportError(f"network error: {exc.reason}") from exc
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return Response(status, {k.lower(): v for k, v in resp_headers.items()}, parsed)
