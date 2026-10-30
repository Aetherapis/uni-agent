"""An in-memory stand-in for the Habitica API, good enough to test the sync end to end."""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field

from uniagent.habitica import Response

LISTS = {"habit": "habits", "daily": "dailys", "todo": "todos"}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


@dataclass
class FakeHabitica:
    clock: FakeClock = field(default_factory=FakeClock)
    user_id: str = "user-1"
    api_key: str = "secret-key"
    move_semantics: str = "after_removal"          # or "before_removal" (to test self-correction)
    create_position: str = "top"                   # or "bottom"
    tasks: dict = field(default_factory=dict)
    order: dict = field(default_factory=lambda: {"habits": [], "dailys": [], "todos": []})
    completed: dict = field(default_factory=dict)
    log: list = field(default_factory=list)        # (time, method, path, headers, body)
    fail_next: list = field(default_factory=list)  # [(status, headers, body)] consumed one per request
    reject_habit_frequency: bool = False
    scores: list = field(default_factory=list)      # (alias or id, direction) for habits/dailies
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # --- helpers for tests ---
    def add_foreign(self, kind: str, text: str, position: str = "top") -> str:
        tid = f"foreign-{next(self._ids)}"
        self.tasks[tid] = {"_id": tid, "type": kind, "text": text, "notes": "", "priority": 1,
                           "completed": False}
        lst = self.order[LISTS[kind]]
        lst.insert(0, tid) if position == "top" else lst.append(tid)
        return tid

    def by_alias(self, alias: str):
        return next((t for t in self.tasks.values() if t.get("alias") == alias), None)

    def texts(self, kind: str) -> list[str]:
        return [self.tasks[i]["text"] for i in self.order[LISTS[kind]]]

    def writes(self) -> list:
        return [e for e in self.log if e[1] in ("POST", "PUT", "DELETE")]

    # --- the Transport protocol ---
    def __call__(self, method, url, headers, body, timeout) -> Response:
        path = url.split("/api/v3", 1)[1]
        self.log.append((self.clock.now(), method, path, dict(headers), body))
        if self.fail_next:
            status, hdrs, payload = self.fail_next.pop(0)
            return Response(status, {k.lower(): v for k, v in hdrs.items()}, payload)
        if headers.get("x-api-user") != self.user_id or headers.get("x-api-key") != self.api_key:
            return Response(401, {}, {"success": False, "error": "NotAuthorized", "message": "bad credentials"})
        if not re.fullmatch(rf"{re.escape(self.user_id)}-[\w-]+", headers.get("x-client", "")):
            return Response(400, {}, {"success": False, "message": "missing x-client"})
        return self._route(method, path, body)

    def _ok(self, data):
        return Response(200, {}, {"success": True, "data": data})

    def _route(self, method, path, body):
        if method == "GET" and path.startswith("/tasks/user"):
            kind = path.split("type=")[1] if "type=" in path else None
            if kind == "completedTodos":
                return self._ok([dict(t) for t in self.completed.values()])
            return self._ok([dict(t) for t in self.tasks.values()])
        if method == "GET" and path.startswith("/user"):
            return self._ok({"tasksOrder": {k: list(v) for k, v in self.order.items()}})
        if method == "POST" and path == "/tasks/user":
            return self._create(body)
        m = re.fullmatch(r"/tasks/([^/]+)(?:/(.*))?", path)
        if not m:
            return Response(404, {}, {"success": False, "message": "no such route"})
        ident, rest = m.group(1), m.group(2)
        task = self.tasks.get(ident) or self.by_alias(ident)
        if task is None:
            return Response(404, {}, {"success": False, "message": "Task not found."})
        tid, kind = task["_id"], task["type"]
        lst = self.order[LISTS[kind]]
        if method == "PUT" and rest is None:
            if self.reject_habit_frequency and kind == "habit" and "frequency" in body:
                return Response(400, {}, {"success": False, "message": "frequency not allowed"})
            task.update(body)
            return self._ok(dict(task))
        if method == "DELETE" and rest is None:
            del self.tasks[tid]
            lst.remove(tid)
            return self._ok({})
        if method == "POST" and rest and rest.startswith("score/") and kind != "todo":
            self.scores.append((task.get("alias") or tid, rest.split("/")[-1]))
            return self._ok({})
        if method == "POST" and rest and rest.startswith("score/"):
            task["completed"] = True
            self.completed[tid] = dict(task)
            del self.tasks[tid]
            lst.remove(tid)
            return self._ok({})
        if method == "POST" and rest and rest.startswith("move/to/"):
            pos = int(rest.split("/")[-1])
            idx = lst.index(tid)
            if self.move_semantics == "after_removal":
                lst.pop(idx)
                lst.append(tid) if pos == -1 else lst.insert(pos, tid)
            else:                                   # "before_removal": index counted with the task still in
                target = len(lst) if pos == -1 else pos
                lst.insert(target, tid)
                lst.pop(idx if idx < target else idx + 1)
            return self._ok(list(lst))
        return Response(404, {}, {"success": False, "message": "no such route"})

    def _create(self, body):
        kind = body.get("type")
        if kind not in LISTS:
            return Response(400, {}, {"success": False, "message": "bad type"})
        alias = body.get("alias")
        if alias and (self.by_alias(alias) or any(t.get("alias") == alias for t in self.completed.values())):
            return Response(400, {}, {"success": False, "message": "alias already used"})
        if self.reject_habit_frequency and kind == "habit" and "frequency" in body:
            return Response(400, {}, {"success": False, "message": "frequency not allowed"})
        tid = f"id-{next(self._ids)}"
        task = {"_id": tid, "notes": "", "priority": 1, "completed": False, **body}
        self.tasks[tid] = task
        lst = self.order[LISTS[kind]]
        lst.insert(0, tid) if self.create_position == "top" else lst.append(tid)
        return self._ok(dict(task))
