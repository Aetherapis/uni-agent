"""Thin, polite Habitica API v3 client (standard library only).

Habitica's rules for automated tools (https://github.com/HabitRPG/habitica/wiki/API-Usage-Guidelines):
  * background scripts wait 30 seconds between API calls - POST, PUT *and* GET;
  * every request carries an ``X-Client`` header "<creator-user-id>-<tool-name>";
  * on HTTP 429 wait for the ``Retry-After`` header.
This client enforces all three. It also takes a wall-clock ``Budget`` so a CI job can never
overrun: when the next call would not fit, it raises ``BudgetExhausted`` and the caller stops
cleanly (the next run simply continues - the sync is state-based and idempotent).

Secrets are never logged and never put in exception messages.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from .httpjson import Response, Transport, TransportError, urllib_transport  # noqa: F401  (re-exported)

log = logging.getLogger("uniagent.habitica")

BASE_URL = "https://habitica.com/api/v3"
DEFAULT_CALL_DELAY_S = 30.0
TASK_LISTS = {"habit": "habits", "daily": "dailys", "todo": "todos"}


class HabiticaError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HabiticaAuthError(HabiticaError): ...
class HabiticaNotFound(HabiticaError): ...
class HabiticaBadRequest(HabiticaError): ...


class HabiticaRateLimited(HabiticaError):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message, 429)
        self.retry_after = retry_after


class BudgetExhausted(Exception):
    """The run's time budget cannot cover the next call."""


class Budget:
    """Wall-clock allowance for one run."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())


@dataclass(frozen=True)
class RemoteTask:
    id: str
    alias: str | None
    type: str                          # habit | daily | todo
    text: str
    notes: str = ""
    priority: float = 1.0
    date: datetime | None = None       # to-dos: due date (aware, UTC)
    frequency: str | None = None
    every_x: int | None = None
    repeat: Mapping[str, bool] = field(default_factory=dict)
    up: bool | None = None
    down: bool | None = None
    completed: bool = False

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> "RemoteTask":
        raw_date = d.get("date")
        due = None
        if raw_date:
            try:
                due = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            except ValueError:
                due = None
        return cls(
            id=str(d.get("_id") or d.get("id")), alias=d.get("alias"), type=str(d.get("type")),
            text=str(d.get("text", "")), notes=str(d.get("notes") or ""),
            priority=float(d.get("priority", 1.0) or 1.0), date=due, frequency=d.get("frequency"),
            every_x=d.get("everyX"), repeat=dict(d.get("repeat") or {}),
            up=d.get("up"), down=d.get("down"), completed=bool(d.get("completed", False)))


class HabiticaClient:
    def __init__(self, user_id: str, api_key: str, *, app_name: str = "uniagent",
                 base_url: str = BASE_URL, call_delay_s: float = DEFAULT_CALL_DELAY_S,
                 transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 3, timeout: float = 30.0,
                 budget: Budget | None = None) -> None:
        if not user_id or not api_key:
            raise HabiticaAuthError("Habitica user id / API key missing")
        self._user_id, self._api_key = user_id, api_key
        self.app_name = app_name
        self.base_url = base_url.rstrip("/")
        self.call_delay_s = call_delay_s
        self.transport, self.clock, self.sleep = transport, clock, sleep
        self.max_retries, self.timeout = max_retries, timeout
        self.budget = budget
        self.calls = 0
        self._last_call: float | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "HabiticaClient":
        env = os.environ if env is None else env
        delay = env.get("HABITICA_CALL_DELAY")
        if delay is not None and "call_delay_s" not in kwargs:
            kwargs["call_delay_s"] = float(delay)
        return cls(env.get("HABITICA_USER_ID", ""), env.get("HABITICA_API_KEY", ""),
                   app_name=env.get("HABITICA_CLIENT_NAME", "uniagent"), **kwargs)

    # ----------------------------------------------------------- plumbing
    def _headers(self) -> dict[str, str]:
        return {"x-api-user": self._user_id, "x-api-key": self._api_key,
                "x-client": f"{self._user_id}-{self.app_name}", "Accept": "application/json"}

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.budget is not None and seconds > self.budget.remaining():
            raise BudgetExhausted(f"next call needs a {seconds:.0f}s pause but the budget is nearly used up")
        self.sleep(seconds)

    def _wait_turn(self) -> None:
        if self.budget is not None and self.budget.remaining() <= 0:
            raise BudgetExhausted("time budget used up")
        if self._last_call is not None:
            self._pause(self._last_call + self.call_delay_s - self.clock())

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        attempt = 0
        while True:
            self._wait_turn()
            try:
                resp = self.transport(method, f"{self.base_url}{path}", self._headers(), body, self.timeout)
            except TransportError as exc:
                raise HabiticaError(str(exc)) from exc
            self._last_call = self.clock()
            self.calls += 1
            if resp.status == 429:
                try:
                    wait = float(resp.headers.get("retry-after", 60))
                except ValueError:
                    wait = 60.0
                attempt += 1
                if attempt > self.max_retries:
                    raise HabiticaRateLimited("rate limited by Habitica", wait)
                log.warning("429 from Habitica; waiting %.0fs", wait)
                self._pause(wait)
                continue
            if resp.status >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise HabiticaError(f"Habitica server error {resp.status}", resp.status)
                self._pause(min(60.0, 5.0 * 2 ** attempt))
                continue
            break

        payload = resp.body if isinstance(resp.body, dict) else {}
        if 200 <= resp.status < 300 and payload.get("success", True):
            return payload.get("data")
        message = str(payload.get("message") or payload.get("error") or f"HTTP {resp.status}")
        if resp.status in (401, 403):
            raise HabiticaAuthError(f"Habitica rejected the credentials ({resp.status})", resp.status)
        if resp.status == 404:
            raise HabiticaNotFound(message, 404)
        if resp.status in (400, 409, 422):
            raise HabiticaBadRequest(message, resp.status)
        raise HabiticaError(message, resp.status)

    # ----------------------------------------------------------- endpoints
    def get_tasks(self, task_type: str | None = None) -> list[RemoteTask]:
        """task_type: habits | dailys | todos | completedTodos (None = all active habits/dailys/todos)."""
        path = "/tasks/user" + (f"?type={task_type}" if task_type else "")
        return [RemoteTask.from_api(t) for t in (self._request("GET", path) or [])
                if t.get("type") in TASK_LISTS]

    def get_tasks_order(self) -> dict[str, list[str]]:
        data = self._request("GET", "/user?userFields=tasksOrder") or {}
        order = data.get("tasksOrder", {})
        return {name: [str(i) for i in order.get(name, [])] for name in TASK_LISTS.values()}

    def create_task(self, payload: dict) -> RemoteTask:
        return RemoteTask.from_api(self._request("POST", "/tasks/user", payload))

    def update_task(self, task_id: str, payload: dict) -> RemoteTask:
        return RemoteTask.from_api(self._request("PUT", f"/tasks/{task_id}", payload))

    def delete_task(self, task_id: str) -> None:
        self._request("DELETE", f"/tasks/{task_id}")

    def score_task(self, task_id: str, direction: str = "up") -> None:
        """Complete a to-do (direction 'up') - this is what grants Habitica's rewards."""
        self._request("POST", f"/tasks/{task_id}/score/{direction}")

    def move_task(self, task_id: str, position: int) -> list[str]:
        """Move to ``position`` (0 = top, -1 = bottom). Returns the task order for that task type."""
        data = self._request("POST", f"/tasks/{task_id}/move/to/{position}")
        return [str(i) for i in data] if isinstance(data, list) else []
