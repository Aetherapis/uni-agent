"""Minimal Notion API client (standard library only) + the database layout the agent uses.

Targets API version 2026-03-11, where a *database* is a container of *data sources*: pages are
queried and created through the data source id. Discovery finds the agent's databases by title
under one parent page, so the only configuration is the integration token and that page's id.

Never logs tokens or page contents.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

from .httpjson import Transport, TransportError, urllib_transport

log = logging.getLogger("uniagent.notion")

NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com/v1"


class NotionError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotionAuthError(NotionError): ...
class NotionNotFound(NotionError): ...


class NotionClient:
    def __init__(self, token: str, *, version: str = NOTION_VERSION, base_url: str = BASE_URL,
                 transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 min_interval_s: float = 0.35, max_retries: int = 4, timeout: float = 30.0) -> None:
        if not token:
            raise NotionAuthError("Notion token missing")
        self._token = token
        self.version, self.base_url = version, base_url.rstrip("/")
        self.transport, self.clock, self.sleep = transport, clock, sleep
        self.min_interval_s, self.max_retries, self.timeout = min_interval_s, max_retries, timeout
        self.calls = 0
        self._last: float | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> "NotionClient":
        env = os.environ if env is None else env
        return cls(env.get("NOTION_TOKEN", ""), **kwargs)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Notion-Version": self.version,
                "Accept": "application/json"}

    def request(self, method: str, path: str, body: dict | None = None, *, safe: bool | None = None) -> Any:
        """``safe`` = the call is read-only, so server errors may be retried (queries are POSTs)."""
        safe = (method == "GET") if safe is None else safe
        attempt = 0
        while True:
            if self._last is not None:                      # stay under Notion's ~3 requests/second
                wait = self._last + self.min_interval_s - self.clock()
                if wait > 0:
                    self.sleep(wait)
            try:
                resp = self.transport(method, f"{self.base_url}{path}", self._headers(), body, self.timeout)
            except TransportError as exc:
                attempt += 1
                if not safe or attempt > self.max_retries:
                    raise NotionError(str(exc)) from exc
                self.sleep(min(30.0, 2.0 ** attempt))
                continue
            self._last = self.clock()
            self.calls += 1
            status = resp.status
            if status in (429, 529) or (safe and status in (500, 502, 503, 504)):
                attempt += 1
                if attempt > self.max_retries:
                    raise NotionError(f"Notion unavailable or rate limited ({status})", status)
                try:
                    wait = float(resp.headers.get("retry-after", 2 ** attempt))
                except ValueError:
                    wait = float(2 ** attempt)
                log.warning("Notion %s; waiting %.0fs", status, wait)
                self.sleep(min(wait, 60.0))
                continue
            break
        payload = resp.body if isinstance(resp.body, dict) else {}
        if 200 <= status < 300:
            return payload
        message = str(payload.get("message") or f"HTTP {status}")
        if status in (401, 403):
            raise NotionAuthError(f"Notion rejected the token or the page isn't shared with the integration ({status})", status)
        if status == 404:
            raise NotionNotFound(f"{message} (is the page shared with the integration?)", 404)
        raise NotionError(message, status)

    # ------------------------------------------------------------- reads
    def query(self, data_source_id: str, *, filter: dict | None = None,
              sorts: Sequence[dict] | None = None) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = list(sorts)
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"/data_sources/{data_source_id}/query", body, safe=True)
            out += data.get("results", [])
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def list_children(self, block_id: str) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self.request("GET", path)
            out += data.get("results", [])
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def retrieve_database(self, database_id: str) -> dict:
        return self.request("GET", f"/databases/{database_id}")

    # ------------------------------------------------------------ writes
    def create_page(self, data_source_id: str, properties: dict) -> dict:
        return self.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties})

    def update_page(self, page_id: str, properties: dict | None = None, *, in_trash: bool | None = None) -> dict:
        body: dict = {}
        if properties is not None:
            body["properties"] = properties
        if in_trash is not None:
            body["in_trash"] = in_trash
        return self.request("PATCH", f"/pages/{page_id}", body)

    def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        return self.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": properties}})


# --------------------------------------------------------------------- layout
# (property name, kind, select options)
Schema = Sequence[tuple[str, str, Sequence[str]]]
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

SCHEMAS: dict[str, Schema] = {
    "Tasks": [
        ("Name", "title", ()), ("Key", "text", ()),
        ("Status", "select", ("Active", "Done", "Cancelled")),
        ("Type", "select", ("To-do", "Daily", "Habit")),
        ("Kind", "select", ("Flexible", "Fixed")),
        ("Priority", "select", ("Urgent", "High", "Normal", "Low")),
        ("Minutes", "number", ()),
        ("Fixed start", "date", ()), ("On date", "date", ()), ("Not before", "date", ()),
        ("Deadline", "date", ()), ("Scheduled", "date", ()),
        ("After", "text", ()), ("Group", "text", ()),
        ("Repeat days", "multi_select", DAYS), ("Daily time", "text", ()), ("Daily time fixed", "checkbox", ()),
        ("Habit +", "checkbox", ()), ("Habit -", "checkbox", ()),
        ("Habit reset", "select", ("daily", "weekly", "monthly")),
        ("Notes", "text", ()), ("Agent note", "text", ()), ("Source", "text", ()), ("Seq", "number", ()),
    ],
    "Timetable": [
        ("Course", "title", ()), ("Kind", "select", ("Lecture", "Tutorial", "Lab")),
        ("Day", "select", DAYS), ("Period", "number", ()), ("Start", "text", ()), ("End", "text", ()),
        ("Location", "text", ()), ("Code", "text", ()), ("Group", "text", ()),
        ("Every weeks", "number", ()), ("Anchor", "date", ()), ("From", "date", ()), ("To", "date", ()),
    ],
    "Buses": [
        ("Route", "title", ()), ("Direction", "select", ("To campus", "To home")),
        ("Depart", "text", ()), ("Arrive", "text", ()), ("Days", "multi_select", DAYS),
    ],
    "Away": [("Label", "title", ()), ("When", "date", ()), ("Load", "number", ())],
    "Exceptions": [("Title", "title", ()), ("Type", "select", ("Cancel class", "Event")),
                   ("When", "date", ()), ("Course", "text", ())],
    "Inbox": [
        ("Text", "title", ()), ("Source", "select", ("text", "voice")), ("Status", "select",
                                                                           ("Pending", "Done", "Error", "Ignored")),
        ("Update id", "number", ()), ("Chat id", "number", ()), ("Message id", "number", ()),
        ("File id", "text", ()), ("Received", "date", ()), ("Result", "text", ()),
    ],
    "State": [("Key", "title", ()), ("Value", "text", ())],
}


def property_schema(kind: str, options: Sequence[str] = ()) -> dict:
    if kind == "title":
        return {"title": {}}
    if kind == "text":
        return {"rich_text": {}}
    if kind == "number":
        return {"number": {"format": "number"}}
    if kind in ("select", "multi_select"):
        return {kind: {"options": [{"name": o} for o in options]}}
    return {kind: {}}                                    # date, checkbox


def schema_properties(table: str) -> dict:
    return {name: property_schema(kind, opts) for name, kind, opts in SCHEMAS[table]}


@dataclass
class Workspace:
    """Data source ids of the agent's databases."""
    ds: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, table: str) -> str:
        return self.ds[table]


def find_databases(client: NotionClient, parent_page_id: str) -> dict[str, str]:
    """Title -> database id for the agent's databases directly under the parent page."""
    found: dict[str, str] = {}
    for block in client.list_children(parent_page_id):
        if block.get("type") == "child_database":
            title = (block.get("child_database") or {}).get("title", "")
            if title in SCHEMAS and title not in found:
                found[title] = block["id"]
    return found


def data_source_of(client: NotionClient, database: dict) -> str:
    sources = database.get("data_sources") or []
    if not sources:
        database = client.retrieve_database(database["id"])
        sources = database.get("data_sources") or []
    if not sources:
        raise NotionError(f"database {database.get('id')} has no data source")
    return str(sources[0]["id"])


def discover(client: NotionClient, parent_page_id: str) -> Workspace:
    found = find_databases(client, parent_page_id)
    missing = [t for t in SCHEMAS if t not in found]
    if missing:
        raise NotionError(f"Notion databases missing under the parent page: {', '.join(missing)} "
                          f"(run the Notion setup script)")
    return Workspace({title: data_source_of(client, {"id": db_id}) for title, db_id in found.items()})
