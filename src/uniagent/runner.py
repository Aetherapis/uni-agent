"""One run of the agent (what GitHub Actions executes every few minutes).

    ingest -> process messages -> plan -> reply -> sync Habitica

Design rules
* Idempotent and crash-safe: Telegram messages go into the Notion Inbox *before* anything else,
  the Telegram offset is saved right after, and the inbox rows are marked Done as soon as their
  commands are applied (writes are at-most-once, so a retry never duplicates a task).
* Cheap when idle: no Gemini call without new messages; Habitica is only touched when the desired
  state changed (or every few hours to catch manual edits). Habitica calls are 30 s apart, so
  skipping them matters.
* Only the owner's private chat is ever obeyed.
* Logs contain counts and exception types only - never task titles, messages or keys.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Mapping

from .actions import ApplyResult, apply_commands
from .config import Rules, load_rules, local_now
from .digest import due_digests, preview, weekly_review
from .gemini import GeminiAuthError, GeminiBadRequest, GeminiClient, GeminiError, GeminiQuotaExhausted
from .habitica import (Budget, HabiticaAuthError, HabiticaBadRequest, HabiticaClient, HabiticaError,
                       HabiticaNotFound)
from .habitica_sync import SyncReport, sync
from .interpreter import Interpretation, interpret
from .models import Kind, Status, Task, TaskType
from .notion import NotionAuthError, NotionClient, NotionError, NotionNotFound, discover
from .notion_setup import parse_page_id
from .notion_store import InboxMessage, NotionStore
from .ordering import build_items, task_key
from .replies import compose_reply, format_agenda, format_status, heads_up, help_text
from .scheduler import ScheduleResult, schedule
from .telegram import TelegramAuthError, TelegramClient, TelegramError, parse_update
from .timetable import Timetable

log = logging.getLogger("uniagent.run")

LOCK_MINUTES = 20
MAX_ATTEMPTS = 3
QUOTA_NOTICE_EVERY = timedelta(hours=3)
HABITICA_VERIFY_EVERY = timedelta(hours=6)


# ------------------------------------------------------------------ services
@dataclass
class Services:
    rules: Rules
    store: NotionStore
    gemini: GeminiClient | None = None
    habitica: HabiticaClient | None = None
    telegram: TelegramClient | None = None
    owner_chat: int | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now())


@dataclass
class RunReport:
    ingested: int = 0
    ignored: int = 0
    processed: int = 0
    slash_commands: int = 0
    replies: int = 0
    plan_writes: int = 0
    gemini_used: bool = False
    habitica: SyncReport | None = None
    habitica_skipped: str = ""
    webhook_mode: bool = False
    busy: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"ingested {self.ingested}", f"processed {self.processed}", f"commands {self.slash_commands}",
                 f"replies {self.replies}", f"notion plan writes {self.plan_writes}"]
        if self.ignored:
            parts.append(f"ignored {self.ignored} from other chats")
        if self.busy:
            parts.append("another run was processing messages")
        if self.webhook_mode:
            parts.append("telegram webhook mode (messages arrive via the relay)")
        if self.habitica is not None:
            parts.append("habitica: " + self.habitica.summary())
        elif self.habitica_skipped:
            parts.append(f"habitica skipped ({self.habitica_skipped})")
        if self.errors:
            parts.append(f"errors {len(self.errors)}")
        return "; ".join(parts)


class Runtime:
    """Small values kept between runs in ONE Notion State row (one read, one write per run)."""
    KEY = "runtime"

    def __init__(self, store: NotionStore) -> None:
        self.store = store
        raw = store.get_state(self.KEY)
        try:
            self.data: dict = json.loads(raw) if raw else {}
        except ValueError:
            self.data = {}
        self._saved = json.dumps(self.data, sort_keys=True)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        if value in (None, "", [], {}):
            self.data.pop(key, None)
        else:
            self.data[key] = value

    def save(self) -> None:
        current = json.dumps(self.data, sort_keys=True)
        if current != self._saved:
            self.store.set_state(self.KEY, current)
            self._saved = current

    def when(self, key: str) -> datetime | None:
        raw = self.data.get(key)
        try:
            return datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None


def _merge_fresh(tasks: list[Task], applied: ApplyResult) -> list[Task]:
    """Notion queries can lag a moment behind writes. For the tasks this very run just created, changed,
    finished or cancelled, trust the in-memory copy (it holds exactly what was written) over the re-read."""
    touched = {t.id: t for t in (*applied.added, *applied.updated, *applied.completed, *applied.cancelled)}
    merged = [touched.pop(t.id, t) for t in tasks]
    merged.extend(touched.values())                               # created, but not returned by the re-read yet
    return merged


# ---------------------------------------------------------------- telegram io
def _send(svc: Services, report: RunReport, text: str) -> bool:
    if svc.telegram is None or svc.owner_chat is None or not text.strip():
        return False
    try:
        svc.telegram.send_message(svc.owner_chat, text)
        report.replies += 1
        return True
    except TelegramAuthError:
        raise
    except TelegramError as exc:
        report.errors.append(f"telegram send failed ({type(exc).__name__})")
        return False


def ingest(svc: Services, rt: Runtime, report: RunReport) -> None:
    """Move new Telegram messages into the Notion Inbox, then confirm them to Telegram."""
    if svc.telegram is None:
        return
    try:
        updates = svc.telegram.get_updates(offset=rt.get("offset"))
    except TelegramAuthError:
        raise
    except TelegramError as exc:
        if exc.status == 409:                                     # a webhook is active: the relay fills the Inbox
            report.webhook_mode = True
            return
        raise
    newest = None
    for update in updates:
        if isinstance(update.get("update_id"), int):
            newest = max(newest or 0, update["update_id"])
        incoming = parse_update(update, svc.rules.tz)
        if incoming is None:
            continue
        if svc.owner_chat is None or incoming.chat_id != svc.owner_chat:
            report.ignored += 1                                   # strangers are silently ignored
            continue
        if incoming.kind == "unsupported":
            _send(svc, report, f"Sorry, {incoming.text}.")
            continue
        stored = svc.store.inbox_add(text=incoming.text, source=incoming.kind, update_id=incoming.update_id,
                                     chat_id=incoming.chat_id, message_id=incoming.message_id,
                                     file_id=incoming.file_id, received=incoming.sent_at)
        report.ingested += int(stored)
    if newest is not None:
        rt.set("offset", newest + 1)
        rt.save()                                                 # confirmed only after everything is stored


# ------------------------------------------------------------ message handling
@dataclass
class Batch:
    interp: Interpretation | None = None
    applied: ApplyResult | None = None
    failed: list[tuple[InboxMessage, str]] = field(default_factory=list)
    slash: list[InboxMessage] = field(default_factory=list)
    replan: bool = False
    force_sync: bool = False
    allow_delete: bool = False
    reload: bool = False
    notices: list[str] = field(default_factory=list)


def _process_natural(svc: Services, rt: Runtime, report: RunReport, natural: list[InboxMessage],
                     tasks: list[Task], tt: Timetable, now: datetime, batch: Batch) -> None:
    if svc.gemini is None:
        batch.notices.append("I can't read messages because no Gemini key is configured.")
        return
    lock_until = rt.when("lock_until")
    if lock_until and lock_until > now:
        report.busy = True
        return
    rt.set("lock_until", (now + timedelta(minutes=LOCK_MINUTES)).isoformat())
    rt.save()
    try:
        def load_audio(msg: InboxMessage):
            assert svc.telegram is not None
            return svc.telegram.download_file(msg.file_id)
        try:
            interp = interpret(svc.gemini, natural, tasks, tt, svc.rules, now,
                               audio_loader=load_audio if svc.telegram else None,
                               pending_questions=rt.get("pending_questions", []))
            report.gemini_used = True
        except GeminiQuotaExhausted:
            last = rt.when("quota_notified_at")
            if last is None or now - last >= QUOTA_NOTICE_EVERY:
                batch.notices.append("I'm out of free AI quota for now. Your messages are saved and I'll process "
                                     "them automatically as soon as it resets.")
                rt.set("quota_notified_at", now.isoformat())
            return
        except (GeminiAuthError, NotionAuthError):
            raise
        except Exception as exc:  # noqa: BLE001 - count the attempt, never lose or loop on a message
            attempts = dict(rt.get("attempts", {}))
            for m in natural[: svc.rules.gemini_max_batch]:
                attempts[str(m.update_id)] = attempts.get(str(m.update_id), 0) + 1
            for m in natural[: svc.rules.gemini_max_batch]:
                if attempts[str(m.update_id)] >= MAX_ATTEMPTS:
                    svc.store.inbox_mark(m, "Error", f"gave up after {MAX_ATTEMPTS} attempts ({type(exc).__name__})")
                    batch.failed.append((m, f"I couldn't understand it after {MAX_ATTEMPTS} tries - please rephrase"))
                    attempts.pop(str(m.update_id), None)
            rt.set("attempts", attempts)
            report.errors.append(f"interpretation failed ({type(exc).__name__})")
            return

        by_update = {m.update_id: m for m in natural}
        for update_id, reason in interp.skipped.items():
            if update_id in by_update:
                svc.store.inbox_mark(by_update[update_id], "Error", reason)
                batch.failed.append((by_update[update_id], reason))
        for update_id, text in interp.transcripts.items():
            if update_id in by_update:
                svc.store.inbox_set_text(by_update[update_id], text)
        sources = {m.update_id: m.text for m in natural}
        sources.update({k: v for k, v in interp.transcripts.items()})
        applied = apply_commands(interp.commands, tasks, tt, svc.store, svc.rules, now, sources)
        for update_id in interp.used:
            msg = by_update[update_id]
            svc.store.inbox_mark(msg, "Done", "; ".join(applied.per_message.get(update_id, [])) or "nothing to do")
        attempts = dict(rt.get("attempts", {}))
        for update_id in interp.used:
            attempts.pop(str(update_id), None)
        rt.set("attempts", attempts)
        rt.set("pending_questions", interp.questions[:3])
        report.processed += len(interp.used)
        batch.interp, batch.applied = interp, applied
        batch.reload = applied.needs_replan
    finally:
        rt.data.pop("lock_until", None)
        rt.save()


def _command_name(text: str) -> tuple[str, str]:
    head, _, rest = text.strip().partition(" ")
    return head.lstrip("/").split("@")[0].lower(), rest.strip()


def _agenda_range(arg: str, today: date) -> tuple[date, int]:
    arg = arg.lower().strip()
    if arg in ("", "today"):
        return today, 1
    if arg == "tomorrow":
        return today + timedelta(days=1), 1
    if arg.isdigit():
        return today, min(7, max(1, int(arg)))
    try:
        return date.fromisoformat(arg), 1
    except ValueError:
        return today, 1


# -------------------------------------------------------------------- digests
def _weekly_text(svc: Services, tasks: list[Task], plan: ScheduleResult, tt: Timetable, now: datetime) -> str:
    done = svc.store.load_recent_tasks(Status.DONE, 7, now=now)
    cancelled = svc.store.load_recent_tasks(Status.CANCELLED, 7, now=now)
    return weekly_review(now, tasks, done, cancelled, plan, tt, svc.rules)


def _send_digests(svc: Services, rt: Runtime, report: RunReport, tasks: list[Task], plan: ScheduleResult,
                  tt: Timetable, now: datetime) -> None:
    if svc.telegram is None:
        return
    sent = dict(rt.get("digests", {}))
    for kind in due_digests(now, svc.rules, sent):
        text = (_weekly_text(svc, tasks, plan, tt, now) if kind == "weekly"
                else preview(kind, now, tasks, plan, tt, svc.rules))
        if text is None or _send(svc, report, text):            # "nothing to say" counts as handled for today
            sent[kind] = now.date().isoformat()
    rt.set("digests", sent)


# ------------------------------------------------------------------- habitica
def fingerprint(items, done_keys) -> str:
    blob = json.dumps([dataclasses.asdict(i) for i in items] + sorted(done_keys), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _sync_habitica(svc: Services, rt: Runtime, report: RunReport, tasks: list[Task], plan: ScheduleResult,
                   tt: Timetable, now: datetime, batch: Batch, forced: bool) -> None:
    if svc.habitica is None:
        report.habitica_skipped = "not configured"
        return
    rules = svc.rules
    items = build_items(tasks, plan.placements, tt, rules, now, plan.daily_times)
    done_keys = {task_key(k) for k in svc.store.load_done_keys()}
    if batch.applied is not None:                                 # finished a moment ago: the re-read may not list it yet
        done_keys |= {task_key(t.id) for t in batch.applied.completed if t.type is TaskType.TODO}
    fp = fingerprint(items, done_keys)
    verified = rt.when("habitica_verified_at")
    stale = verified is None or now - verified >= HABITICA_VERIFY_EVERY
    if not (forced or batch.force_sync or fp != rt.get("habitica_fp") or stale):
        report.habitica_skipped = "nothing changed"
    else:
        budget = Budget(rules.habitica_budget_min * 60)
        result = sync(svc.habitica, items, rules.tz, today=now.date(), done_keys=done_keys, budget=budget,
                      max_deletes=rules.habitica_max_deletes, allow_mass_delete=batch.allow_delete,
                      expected_deletes={task_key(t.id) for t in batch.applied.cancelled} if batch.applied else ())
        report.habitica = result
        by_key = {task_key(t.id): t for t in tasks}
        for key in result.completed_on_habitica:                  # ticked off in the Habitica app
            if key in by_key:
                svc.store.set_status(by_key[key], Status.DONE)
        clean = not (result.failed or result.blocked or result.stopped_early)
        rt.set("habitica_fp", fp if clean else None)
        rt.set("habitica_verified_at", now.isoformat() if clean else rt.get("habitica_verified_at"))
        alert = None
        if result.blocked:
            alert = f"Habitica sync paused: {result.blocked}. If that's intended, send /sync force."
        elif result.failed:
            alert = f"Habitica sync had {len(result.failed)} problem(s); I'll retry on the next run."
        alert_fp = hashlib.sha256((alert or "").encode()).hexdigest()[:12] if alert else ""
        if alert and alert_fp != rt.get("habitica_alert"):
            _send(svc, report, alert)
        rt.set("habitica_alert", alert_fp)
    if batch.applied is not None and batch.applied.check_ins:
        svc.habitica.budget = Budget(rules.habitica_budget_min * 60)
        for task, direction in batch.applied.check_ins:
            try:
                svc.habitica.score_task(task_key(task.id), direction)
            except HabiticaAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"could not log a check-in ({type(exc).__name__})")
                _send(svc, report, f"I couldn't log '{task.title}' in Habitica - tick it there for now.")


# ------------------------------------------------------------------- the run
def run_once(svc: Services, *, skip_habitica: bool = False, sync_only: bool = False,
             force_sync: bool = False) -> RunReport:
    report = RunReport()
    rt = Runtime(svc.store)
    now = svc.now()
    batch = Batch()
    try:
        tasks: list[Task] | None = None
        tt: Timetable | None = None
        pending: list[InboxMessage] = []
        if not sync_only:
            ingest(svc, rt, report)
            pending = svc.store.inbox_pending()
            natural = [m for m in pending if not m.text.startswith("/")]
            batch.slash = [m for m in pending if m.text.startswith("/")]
            for m in batch.slash:                                 # flags that change planning come first
                name, arg = _command_name(m.text)
                batch.replan |= name == "replan"
                batch.force_sync |= name == "sync"
                batch.allow_delete |= name == "sync" and arg.lower() == "force"
            if natural:
                tasks, tt = svc.store.load_tasks(), svc.store.load_timetable()
                _process_natural(svc, rt, report, natural, tasks, tt, now, batch)
        if tasks is None or tt is None or batch.reload:
            tasks, tt = svc.store.load_tasks(), svc.store.load_timetable()
            if batch.applied is not None:
                tasks = _merge_fresh(tasks, batch.applied)
        if batch.replan:
            for t in tasks:
                if t.type is TaskType.TODO and t.kind is Kind.FLEXIBLE:
                    t.placement = None
        plan = schedule(tasks, tt, svc.rules, now)
        report.plan_writes = svc.store.save_schedule(tasks, plan)

        replied = False
        if batch.interp is not None or batch.failed or batch.notices:
            lines: list[str] = []
            if batch.interp is not None and batch.applied is not None:
                lines.append(compose_reply(batch.interp, batch.applied, tasks, plan, tt, svc.rules, now))
            lines += [f"Couldn't process a message: {why}" for _, why in batch.failed]
            lines += batch.notices
            _send(svc, report, "\n".join(lines))
            replied = True
        for m in batch.slash:
            name, arg = _command_name(m.text)
            reply = {
                "start": help_text(), "help": help_text(),
                "agenda": None, "status": None, "digest": None, "review": None,
                "replan": "Re-planning everything from scratch.", "sync": "Syncing Habitica now.",
            }.get(name, f"I don't know /{name}. Try /help.")
            if name == "agenda":
                day, count = _agenda_range(arg, now.date())
                reply = format_agenda(day, count, tasks, plan, tt, svc.rules)
            elif name == "digest":
                reply = preview("evening", now, tasks, plan, tt, svc.rules, force=True)
            elif name == "review":
                reply = _weekly_text(svc, tasks, plan, tt, now)
            elif name == "status":
                reply = format_status(tasks, plan, now, pending=sum(1 for p in pending if p.status == "Pending" and not p.text.startswith("/")),
                                      last_sync=rt.when("habitica_verified_at"))
            _send(svc, report, reply)
            svc.store.inbox_mark(m, "Done", name)
            report.slash_commands += 1

        digest = heads_up(plan)
        digest_fp = hashlib.sha256("\n".join(digest).encode()).hexdigest()[:12] if digest else ""
        if digest and digest_fp != rt.get("warn_fp") and not replied:
            _send(svc, report, "Heads up:\n" + "\n".join(f"- {d}" for d in digest))
        rt.set("warn_fp", digest_fp)
        _send_digests(svc, rt, report, tasks, plan, tt, now)
        rt.save()

        if skip_habitica:
            report.habitica_skipped = "disabled for this run"
        else:
            _sync_habitica(svc, rt, report, tasks, plan, tt, now, batch, force_sync)
    finally:
        rt.data.pop("lock_until", None)
        rt.save()
    log.info("run finished: %s", report.summary())
    return report


# ---------------------------------------------------------------------- main
def build_services(env: Mapping[str, str], rules: Rules, *, need_telegram: bool = True) -> Services:
    page = env.get("NOTION_PARENT_PAGE", "")
    if not page:
        raise SystemExit("NOTION_PARENT_PAGE is not set")
    notion = NotionClient.from_env(env)
    store = NotionStore(notion, discover(notion, parse_page_id(page)), rules)
    telegram = TelegramClient.from_env(env) if env.get("TELEGRAM_BOT_TOKEN") else None
    owner = env.get("TELEGRAM_CHAT_ID", "")
    if need_telegram and (telegram is None or not owner.lstrip("-").isdigit()):
        raise SystemExit("TELEGRAM_BOT_TOKEN and a numeric TELEGRAM_CHAT_ID are required")
    return Services(
        rules=rules, store=store,
        gemini=GeminiClient.from_env(env, models=rules.gemini_models) if env.get("GEMINI_API_KEY") else None,
        habitica=HabiticaClient.from_env(env, call_delay_s=rules.habitica_call_delay_s) if env.get("HABITICA_USER_ID") else None,
        telegram=telegram, owner_chat=int(owner) if owner.lstrip("-").isdigit() else None,
        now=lambda: local_now(rules))


def is_transient(exc: BaseException) -> bool:
    """Network blips, rate limits and server errors: worth a quiet retry on the next run."""
    if not isinstance(exc, (NotionError, HabiticaError, GeminiError, TelegramError)):
        return False
    if isinstance(exc, (NotionAuthError, NotionNotFound, HabiticaAuthError, HabiticaNotFound, HabiticaBadRequest,
                        GeminiAuthError, GeminiBadRequest, TelegramAuthError)):
        return False
    status = getattr(exc, "status", None)
    return status is None or status == 429 or status >= 500


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the university task agent once.")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--skip-habitica", action="store_true", help="plan and reply but don't touch Habitica")
    ap.add_argument("--sync-only", action="store_true", help="skip Telegram/Gemini; just re-plan and sync")
    ap.add_argument("--force-sync", action="store_true", help="sync Habitica even if nothing changed")
    ap.add_argument("--debug", action="store_true", help="print full tracebacks (don't use in a public repo)")
    args = ap.parse_args(argv)
    debug = args.debug or os.environ.get("UNIAGENT_DEBUG") == "1"
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    rules = load_rules(args.config)
    try:
        svc = build_services(os.environ, rules, need_telegram=not args.sync_only)
        report = run_once(svc, skip_habitica=args.skip_habitica, sync_only=args.sync_only, force_sync=args.force_sync)
    except (NotionAuthError, HabiticaAuthError, GeminiAuthError, TelegramAuthError) as exc:
        print(f"Authentication problem with {type(exc).__name__.replace('AuthError', '')}: check that secret.")
        return 1
    except Exception as exc:  # noqa: BLE001 - never print messages: they could contain private text in public logs
        if debug:
            traceback.print_exc()
        if is_transient(exc):
            print(f"Temporary problem ({type(exc).__name__}); the next run will retry.")
            return 0
        print(f"Run failed: {type(exc).__name__} (turn on debug for details)")
        return 2
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
