"""A strict in-memory Notion (API 2026-03-11 data-source model) for tests.

Strict on purpose: unknown property names, wrong value shapes for a property's type, a missing
data_source_id parent, or the wrong Notion-Version header all return 400 - so typos in the
code's column names or payload shapes fail the tests instead of the real workspace.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from uniagent.httpjson import Response
from uniagent.notion import NOTION_VERSION

KIND_KEY = {"title": "title", "text": "rich_text", "number": "number", "select": "select",
            "multi_select": "multi_select", "date": "date", "checkbox": "checkbox"}


def _err(status, message, code="validation_error"):
    return Response(status, {}, {"object": "error", "status": status, "code": code, "message": message})


@dataclass
class FakeNotion:
    token: str = "ntn_secret"
    page_size: int = 100
    create_returns_sources: bool = True
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc))
    log: list = field(default_factory=list)
    fail_next: list = field(default_factory=list)
    known_pages: set = field(default_factory=lambda: {"parent-page"})
    databases: dict = field(default_factory=dict)      # db id -> {title, parent, ds}
    sources: dict = field(default_factory=dict)        # ds id -> {schema: {name: kind}, pages: [ids]}
    pages: dict = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _id(self, prefix):
        return f"{prefix}{next(self._ids):04d}"

    def tick(self, **kw):
        self.now += timedelta(**kw)

    def rows(self, db_title):
        ds = next(d["ds"] for d in self.databases.values() if d["title"] == db_title)
        return [self.pages[i] for i in self.sources[ds]["pages"] if not self.pages[i]["in_trash"]]

    def writes(self):
        return [e for e in self.log if e[0] in ("POST", "PATCH") and "/query" not in e[1]]

    # ---- Transport ----
    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/v1", 1)[1]
        self.log.append((method, path, body))
        if self.fail_next:
            status, hdrs, payload = self.fail_next.pop(0)
            return Response(status, {k.lower(): v for k, v in hdrs.items()}, payload)
        if headers.get("Authorization") != f"Bearer {self.token}":
            return _err(401, "API token is invalid.", "unauthorized")
        if headers.get("Notion-Version") != NOTION_VERSION:
            return _err(400, "wrong Notion-Version")
        return self._route(method, path, body or {})

    def _route(self, method, path, body):
        if method == "GET" and (m := re.fullmatch(r"/blocks/([^/?]+)/children(?:\?(.*))?", path)):
            blocks = [{"object": "block", "id": i, "type": "child_database",
                       "child_database": {"title": d["title"]}}
                      for i, d in self.databases.items() if d["parent"] == m.group(1)]
            blocks.insert(0, {"object": "block", "id": "para-1", "type": "paragraph", "paragraph": {}})
            if m.group(1) not in self.known_pages:
                return _err(404, "Could not find block", "object_not_found")
            return self._paged(blocks, m.group(2) or "")
        if method == "GET" and (m := re.fullmatch(r"/databases/([^/]+)", path)):
            db = self.databases.get(m.group(1))
            if not db:
                return _err(404, "Could not find database", "object_not_found")
            return Response(200, {}, {"object": "database", "id": m.group(1),
                                      "data_sources": [{"id": db["ds"], "name": db["title"]}]})
        if method == "POST" and path == "/databases":
            return self._create_database(body)
        if method == "POST" and (m := re.fullmatch(r"/data_sources/([^/]+)/query", path)):
            return self._query(m.group(1), body)
        if method == "POST" and path == "/pages":
            return self._create_page(body)
        if method == "PATCH" and (m := re.fullmatch(r"/pages/([^/]+)", path)):
            return self._patch_page(m.group(1), body)
        return _err(404, f"no route {method} {path}", "invalid_request_url")

    def _paged(self, items, query):
        cursor = int(re.search(r"start_cursor=(\d+)", query).group(1)) if "start_cursor=" in query else 0
        chunk = items[cursor:cursor + self.page_size]
        more = cursor + self.page_size < len(items)
        return Response(200, {}, {"object": "list", "results": chunk, "has_more": more,
                                  "next_cursor": str(cursor + self.page_size) if more else None})

    def _create_database(self, body):
        props = (body.get("initial_data_source") or {}).get("properties")
        if not props:
            return _err(400, "properties must be nested under initial_data_source")
        title = body["title"][0]["text"]["content"]
        schema = {}
        for name, spec in props.items():
            kinds = [k for k, v in KIND_KEY.items() if v in spec]
            if len(kinds) != 1:
                return _err(400, f"bad property schema for {name}")
            schema[name] = kinds[0]
        db_id, ds_id = self._id("db-"), self._id("ds-")
        self.databases[db_id] = {"title": title, "parent": body["parent"]["page_id"], "ds": ds_id}
        self.sources[ds_id] = {"schema": schema, "pages": []}
        out = {"object": "database", "id": db_id}
        if self.create_returns_sources:
            out["data_sources"] = [{"id": ds_id, "name": title}]
        return Response(200, {}, out)

    def _normalize(self, schema, props):
        out = {}
        for name, value in props.items():
            kind = schema.get(name)
            if kind is None:
                return None, f"property '{name}' does not exist"
            key = KIND_KEY[kind]
            if not isinstance(value, dict) or key not in value:
                return None, f"property '{name}' should be a {kind} value"
            v = value[key]
            if kind in ("title", "text"):
                texts = [t["text"]["content"] for t in v]
                out[name] = {"type": key, key: [{"plain_text": c, "text": {"content": c}} for c in texts]}
            elif kind == "select":
                out[name] = {"type": key, key: ({"name": v["name"]} if v else None)}
            elif kind == "multi_select":
                out[name] = {"type": key, key: [{"name": o["name"]} for o in v]}
            elif kind == "date":
                if v is not None and "start" not in v:
                    return None, f"date '{name}' needs start"
                out[name] = {"type": key, key: v}
            else:
                out[name] = {"type": key, key: v}
        return out, None

    def _create_page(self, body):
        parent = body.get("parent", {})
        ds = parent.get("data_source_id")
        if not ds or ds not in self.sources:
            return _err(400, "parent must be a valid data_source_id")
        schema = self.sources[ds]["schema"]
        props, err = self._normalize(schema, body.get("properties", {}))
        if err:
            return _err(400, err)
        empty = {"title": [], "text": [], "number": None, "select": None, "multi_select": [], "date": None,
                 "checkbox": False}
        for name, kind in schema.items():                      # real Notion returns every column, empty if unset
            props.setdefault(name, {"type": KIND_KEY[kind], KIND_KEY[kind]: empty[kind]})
        page_id = self._id("page-")
        stamp = self.now.isoformat().replace("+00:00", ".000Z")
        page = {"object": "page", "id": page_id, "created_time": stamp, "last_edited_time": stamp,
                "in_trash": False, "ds": ds, "properties": props}
        self.pages[page_id] = page
        self.sources[ds]["pages"].append(page_id)
        return Response(200, {}, dict(page))

    def _patch_page(self, page_id, body):
        page = self.pages.get(page_id)
        if page is None:
            return _err(404, "Could not find page", "object_not_found")
        if "properties" in body:
            props, err = self._normalize(self.sources[page["ds"]]["schema"], body["properties"])
            if err:
                return _err(400, err)
            page["properties"].update(props)
        if "in_trash" in body:
            page["in_trash"] = body["in_trash"]
        page["last_edited_time"] = self.now.isoformat().replace("+00:00", ".000Z")
        return Response(200, {}, dict(page))

    def _match(self, page, flt):
        if "and" in flt:
            return all(self._match(page, f) for f in flt["and"])
        if "or" in flt:
            return any(self._match(page, f) for f in flt["or"])
        if flt.get("timestamp") == "last_edited_time":
            cutoff = datetime.fromisoformat(flt["last_edited_time"]["on_or_after"])
            return datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00")) >= cutoff
        prop = page["properties"].get(flt["property"], {})
        if "select" in flt:
            current = (prop.get("select") or {}).get("name")
            cond = flt["select"]
            return (current is None) if cond.get("is_empty") else current == cond.get("equals")
        if "number" in flt:
            return prop.get("number") == flt["number"]["equals"]
        if "title" in flt:
            return "".join(t["plain_text"] for t in prop.get("title", [])) == flt["title"]["equals"]
        raise AssertionError(f"fake does not support filter {flt}")

    def _query(self, ds, body):
        if ds not in self.sources:
            return _err(404, "Could not find data source", "object_not_found")
        rows = [self.pages[i] for i in self.sources[ds]["pages"] if not self.pages[i]["in_trash"]]
        if body.get("filter"):
            rows = [p for p in rows if self._match(p, body["filter"])]
        for sort in reversed(body.get("sorts", [])):
            def key(p, name=sort["property"]):
                v = p["properties"].get(name, {}).get("number")
                return (v is not None, v or 0)
            rows.sort(key=key, reverse=sort["direction"] == "descending")
        cursor = int(body.get("start_cursor") or 0)
        chunk = rows[cursor:cursor + self.page_size]
        more = cursor + self.page_size < len(rows)
        return Response(200, {}, {"object": "list", "results": [dict(p) for p in chunk], "has_more": more,
                                  "next_cursor": str(cursor + self.page_size) if more else None})
