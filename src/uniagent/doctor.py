"""Pre-flight check: is everything configured and reachable?  python -m uniagent.doctor [--ai] [--audio] [--ping]

Prints one line per check and exits non-zero if any failed. Output contains counts and error
types only - never keys, tokens, task titles or message text - so it is safe in public CI logs.
It makes the fewest calls it can (Habitica asks for 30 s between calls): one Habitica call, a few
Notion calls, one Telegram call. ``--ai`` makes one tiny Gemini request, ``--audio`` sends a
1-second silent voice note in Telegram's format to confirm Gemini accepts it.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .config import ConfigError, Rules, load_rules
from .gemini import GeminiClient, audio_part, text_part
from .habitica import HabiticaClient
from .habitica_sync import is_managed
from .notion import SCHEMAS, NotionClient, discover
from .notion_setup import parse_page_id
from .notion_store import NotionStore
from .telegram import TelegramClient

REQUIRED_ENV = ("NOTION_TOKEN", "NOTION_PARENT_PAGE", "HABITICA_USER_ID", "HABITICA_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY")
OPTIONAL_ENV = ("HABITICA_CALL_DELAY", "HABITICA_CLIENT_NAME", "UNIAGENT_CONFIG", "UNIAGENT_DEBUG",
                "TELEGRAM_WEBHOOK_SECRET")
SILENCE = Path(__file__).parent / "assets" / "silence.oga"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _attempt(name: str, fn: Callable[[], str]) -> Check:
    try:
        return Check(name, True, fn())
    except Exception as exc:  # noqa: BLE001 - a check must never crash the doctor
        text = str(exc).replace("\n", " ")[:200]
        return Check(name, False, f"{type(exc).__name__}: {text}")


def run_doctor(rules: Rules, env: Mapping[str, str], *, notion: NotionClient | None = None,
               habitica: HabiticaClient | None = None, telegram: TelegramClient | None = None,
               gemini: GeminiClient | None = None, ai: bool = False, audio: bool = False,
               ping: bool = False) -> list[Check]:
    checks: list[Check] = []
    missing = [n for n in REQUIRED_ENV if not env.get(n)]
    checks.append(Check("settings", not missing,
                        "all 7 secrets are set" if not missing else "missing: " + ", ".join(missing)))
    checks.append(Check("config", True, f"timezone {rules.timezone}, {rules.max_task_minutes_per_day} min/day cap"))

    # ---- Notion: reachable, databases present, readable AND writable ----
    store_holder: dict[str, NotionStore] = {}

    def notion_check() -> str:
        client = notion or NotionClient.from_env(env)
        ws = discover(client, parse_page_id(env.get("NOTION_PARENT_PAGE", "")))
        store = NotionStore(client, ws, rules)
        store_holder["store"] = store
        tt = store.load_timetable()
        tasks = store.load_tasks()
        problems = len(store.problems)
        probe = client.create_page(ws["State"], {"Key": {"title": [{"text": {"content": "doctor-check"}}]}})
        client.update_page(probe["id"], in_trash=True)                       # write access + trash both work
        return (f"{len(SCHEMAS)} databases, {len(tt.sessions)} classes, {len(tt.buses)} bus trips, "
                f"{len(tasks)} active tasks, {problems} unusable row(s), write access ok")

    checks.append(_attempt("notion", notion_check))
    store = store_holder.get("store")
    if store is not None:
        checks.append(Check("timetable", len(store.load_timetable().sessions) > 0,
                            "classes loaded" if store.load_timetable().sessions else
                            "the Timetable database is empty - run the Notion setup with --seed"))

    # ---- Habitica: one call ----
    def habitica_check() -> str:
        client = habitica or HabiticaClient.from_env(env, call_delay_s=rules.habitica_call_delay_s)
        tasks = client.get_tasks()
        return f"credentials ok, {len(tasks)} tasks ({sum(is_managed(t.alias) for t in tasks)} managed by the agent)"

    checks.append(_attempt("habitica", habitica_check))

    # ---- Telegram ----
    def telegram_check() -> str:
        client = telegram or TelegramClient.from_env(env)
        me = client.get_me()
        owner = env.get("TELEGRAM_CHAT_ID", "")
        if not owner.lstrip("-").isdigit():
            raise ConfigError("TELEGRAM_CHAT_ID must be a number")
        if ping:
            client.send_message(int(owner), "Doctor check: Telegram works.")
        info = client.get_webhook_info()
        if info.get("url"):
            host = info["url"].split("/")[2] if "//" in info["url"] else "?"
            mode = f"messages arrive via webhook ({host}), {info.get('pending_update_count', 0)} waiting"
            if info.get("last_error_message"):
                mode += f", last delivery error: {str(info['last_error_message'])[:80]}"
        else:
            mode = "messages are polled (no webhook)"
        return f"bot @{me.get('username', '?')} ok, {mode}" + (", test message sent" if ping else "")

    checks.append(_attempt("telegram", telegram_check))

    # ---- Gemini (optional: costs quota) ----
    schema = {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}, "heard": {"type": "STRING"}},
              "required": ["ok"]}
    if ai or audio:
        client = gemini or None

        def gemini_check() -> str:
            c = client or GeminiClient.from_env(env, models=rules.gemini_models)
            data, model = c.generate_json("Reply with JSON only.", [text_part('Return {"ok": true}.')], schema)
            if data.get("ok") is not True:
                raise ValueError("unexpected answer shape")
            return f"key ok, model {model} answered in JSON"

        checks.append(_attempt("gemini", gemini_check))

    if audio:
        def audio_check() -> str:
            c = client or GeminiClient.from_env(env, models=rules.gemini_models)
            parts = [text_part('This is a test clip. Return {"ok": true, "heard": "<what you hear, or empty if silence>"}.'),
                     audio_part(SILENCE.read_bytes(), "audio/ogg")]
            _, model = c.generate_json("Reply with JSON only.", parts, schema)
            return f"voice notes in Telegram's format (audio/ogg) are accepted by {model}"

        checks.append(_attempt("gemini-audio", audio_check))
    return checks


def format_checks(checks: list[Check]) -> str:
    return "\n".join(f"[{'OK' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in checks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--ai", action="store_true", help="also test Gemini (uses one request of free quota)")
    ap.add_argument("--audio", action="store_true", help="also test that Gemini accepts Telegram voice notes")
    ap.add_argument("--ping", action="store_true", help="send a test message to your Telegram chat")
    args = ap.parse_args(argv)
    try:
        rules = load_rules(args.config)
    except ConfigError as exc:
        print(f"[FAIL] config: {exc}")
        return 1
    checks = run_doctor(rules, os.environ, ai=args.ai, audio=args.audio, ping=args.ping)
    print(format_checks(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
