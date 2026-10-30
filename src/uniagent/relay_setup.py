"""Switch between polling and the instant-reply relay (a Cloudflare Worker that receives Telegram's
webhook, stores the message in the Notion Inbox and wakes the GitHub workflow).

    python -m uniagent.relay_setup info
    python -m uniagent.relay_setup set --url https://my-relay.example.workers.dev --secret <random>
    python -m uniagent.relay_setup clear          # back to polling
    python -m uniagent.relay_setup inbox-id       # the Inbox data source id the Worker needs

Prints the host and counts only - never the secret or the bot token.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Mapping

from .notion import NotionClient, discover
from .notion_setup import parse_page_id
from .telegram import TelegramClient, TelegramError

SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,256}")


def _host(url: str) -> str:
    return url.split("/")[2] if "//" in url else url


def run(command: str, *, telegram: TelegramClient | None = None, notion: NotionClient | None = None,
        env: Mapping[str, str] | None = None, url: str = "", secret: str = "") -> tuple[int, str]:
    env = os.environ if env is None else env
    if command == "inbox-id":
        client = notion or NotionClient.from_env(env)
        ws = discover(client, parse_page_id(env.get("NOTION_PARENT_PAGE", "")))
        return 0, f"NOTION_INBOX_DS={ws['Inbox']}"
    tg = telegram or TelegramClient.from_env(env)
    if command == "info":
        info = tg.get_webhook_info()
        if not info.get("url"):
            return 0, "polling mode (no webhook set)"
        text = f"webhook mode: {_host(info['url'])}, {info.get('pending_update_count', 0)} update(s) waiting"
        if info.get("last_error_message"):
            text += f"; last delivery error: {str(info['last_error_message'])[:100]}"
        return 0, text
    if command == "clear":
        tg.delete_webhook()
        return 0, "webhook removed: the scheduled runs will poll Telegram again"
    if command == "set":
        secret = secret or env.get("TELEGRAM_WEBHOOK_SECRET", "")
        if not url.startswith("https://"):
            return 1, "the relay URL must start with https://"
        if not SECRET_RE.fullmatch(secret):
            return 1, "the webhook secret must be 16-256 characters: letters, digits, '-' or '_'"
        tg.set_webhook(url, secret)
        return 0, f"webhook set to {_host(url)}: messages now arrive instantly via the relay"
    return 1, f"unknown command {command!r}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["info", "set", "clear", "inbox-id"])
    ap.add_argument("--url", default="")
    ap.add_argument("--secret", default="")
    args = ap.parse_args(argv)
    try:
        code, message = run(args.command, url=args.url, secret=args.secret)
    except TelegramError as exc:
        code, message = 1, f"Telegram refused: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 - never print messages that could hold secrets
        code, message = 1, f"failed: {type(exc).__name__}"
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
