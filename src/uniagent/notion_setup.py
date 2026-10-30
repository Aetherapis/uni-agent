"""Create the agent's Notion databases (idempotent) and import the seed timetable.

    python -m uniagent.notion_setup --parent <page id or URL> [--seed seed/timetable.toml]

Reads NOTION_TOKEN from the environment. Existing databases are left alone; the seed is only
imported into empty tables.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .config import Rules, load_rules
from .loaders import load_toml, timetable_from_dict
from .notion import (SCHEMAS, NotionClient, Workspace, data_source_of, find_databases, schema_properties)
from .notion_store import NotionStore


def parse_page_id(text: str) -> str:
    """Accepts a bare id or a Notion page URL and returns the 32-hex id."""
    match = re.findall(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                       text.replace("?", " ").split("#")[0])
    if not match:
        raise ValueError("could not find a Notion page id in that text")
    return match[-1].replace("-", "").lower()


def setup(client: NotionClient, parent_page_id: str, rules: Rules, *, seed_path: str | Path | None = None) -> dict:
    """Returns {"created": [...], "existing": [...], "seeded": bool}."""
    found = find_databases(client, parent_page_id)
    created: list[str] = []
    sources: dict[str, str] = {}
    for table in SCHEMAS:
        if table in found:
            sources[table] = data_source_of(client, {"id": found[table]})
        else:
            db = client.create_database(parent_page_id, table, schema_properties(table))
            sources[table] = data_source_of(client, db)
            created.append(table)
    store = NotionStore(client, Workspace(sources), rules)
    seeded = False
    if seed_path:
        empty = not client.query(sources["Timetable"]) and not client.query(sources["Buses"])
        if empty:
            store.seed_timetable(timetable_from_dict(load_toml(seed_path), rules))
            seeded = True
    return {"created": created, "existing": [t for t in SCHEMAS if t not in created], "seeded": seeded}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--parent", required=True, help="Notion page id or URL that will hold the databases")
    ap.add_argument("--seed", help="timetable TOML to import into empty tables")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    rules = load_rules(args.config)
    report = setup(NotionClient.from_env(), parse_page_id(args.parent), rules, seed_path=args.seed)
    print(f"created: {', '.join(report['created']) or 'nothing'}; already there: "
          f"{', '.join(report['existing']) or 'nothing'}; timetable imported: {report['seeded']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
