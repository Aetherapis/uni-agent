"""Make Habitica match what the scheduler wants.

The sync is STATE-BASED: every run compares "what Habitica should contain" (``HabiticaItem``s)
with "what it contains now" and performs only the difference. So it is idempotent, and a run
that is cut short by its time budget simply continues next time - no queue to persist.

Safety rules
* Only tasks whose alias starts with ``t-`` or ``c-`` (ours) are ever changed or deleted.
  Your own Habitica tasks are never touched - they just sit below ours in each list.
* A mass delete (or an empty "desired" list while Habitica has ours) is refused and reported:
  that pattern usually means a data-source failure, not a real request.
* Updates send only the fields that changed (streaks etc. are never reset).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .habitica import (TASK_LISTS, Budget, BudgetExhausted, HabiticaAuthError, HabiticaBadRequest,
                       HabiticaClient, HabiticaError, HabiticaNotFound, RemoteTask)
from .ordering import HabiticaItem, apply_moves, plan_moves

log = logging.getLogger("uniagent.sync")

MANAGED_PREFIXES = ("t-", "c-")
REPEAT_KEYS = ("m", "t", "w", "th", "f", "s", "su")        # Monday .. Sunday
MAX_MOVES_PER_LIST = 25


def is_managed(alias: str | None) -> bool:
    return bool(alias) and alias.startswith(MANAGED_PREFIXES)


# ------------------------------------------------------------------ payloads
def _utc_iso(local_naive: datetime, tz: ZoneInfo) -> str:
    return local_naive.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _todo_due(item: HabiticaItem) -> datetime | None:
    if item.start is not None:
        return item.start
    if item.due is not None:
        return datetime.combine(item.due, time(23, 59))
    return None


def desired_fields(item: HabiticaItem, tz: ZoneInfo) -> dict:
    """The Habitica fields this item should have (None = "no value")."""
    f: dict = {"text": item.title.strip(), "notes": item.notes.strip(), "priority": item.difficulty}
    if item.kind == "todo":
        due = _todo_due(item)
        f["date"] = _utc_iso(due, tz) if due else None
    elif item.kind == "daily":
        if item.repeat_days:
            f.update(frequency="weekly", everyX=1,
                     repeat={k: (i in item.repeat_days) for i, k in enumerate(REPEAT_KEYS)})
        else:
            f.update(frequency="daily", everyX=1, repeat={k: True for k in REPEAT_KEYS})
    elif item.kind == "habit":
        f.update(up=item.up, down=item.down, frequency=item.reset)
    return f


def create_payload(item: HabiticaItem, tz: ZoneInfo, today: date) -> dict:
    payload = {"type": item.kind, "alias": item.key,
               **{k: v for k, v in desired_fields(item, tz).items() if v is not None}}
    if item.kind == "daily":
        payload["startDate"] = _utc_iso(datetime.combine(today, time(0, 0)), tz)
    return payload


def update_payload(item: HabiticaItem, remote: RemoteTask, tz: ZoneInfo) -> dict:
    """Only the fields that differ between what we want and what Habitica has."""
    want = desired_fields(item, tz)
    changed: dict = {}
    if remote.text.strip() != want["text"]:
        changed["text"] = want["text"]
    if remote.notes.strip() != want["notes"]:
        changed["notes"] = want["notes"]
    if abs(float(remote.priority) - float(want["priority"])) > 1e-6:
        changed["priority"] = want["priority"]

    if item.kind == "todo":
        due = _todo_due(item)
        remote_day = remote.date.astimezone(tz).date() if remote.date else None
        if (due.date() if due else None) != remote_day:          # only the DAY matters to Habitica
            changed["date"] = want["date"]
    elif item.kind == "daily":
        remote_repeat = {k: bool(remote.repeat.get(k, False)) for k in REPEAT_KEYS}
        if (remote.frequency != want["frequency"] or (remote.every_x or 1) != 1
                or (want["frequency"] == "weekly" and remote_repeat != want["repeat"])):
            changed.update(frequency=want["frequency"], everyX=1, repeat=want["repeat"])
    elif item.kind == "habit":
        if remote.up is not None and bool(remote.up) != want["up"]:
            changed["up"] = want["up"]
        if remote.down is not None and bool(remote.down) != want["down"]:
            changed["down"] = want["down"]
        if remote.frequency is not None and remote.frequency != want["frequency"]:
            changed["frequency"] = want["frequency"]
    return changed


# ---------------------------------------------------------------------- plan
@dataclass
class Plan:
    scores: list[RemoteTask] = field(default_factory=list)                   # to-dos to complete (done in chat)
    deletes: list[RemoteTask] = field(default_factory=list)
    creates: list[HabiticaItem] = field(default_factory=list)
    updates: list[tuple[RemoteTask, HabiticaItem, dict]] = field(default_factory=list)
    completed_keys: list[str] = field(default_factory=list)                  # finished in Habitica itself
    blocked: str | None = None

    @property
    def writes(self) -> int:
        return len(self.scores) + len(self.deletes) + len(self.creates) + len(self.updates)


def build_plan(desired: Sequence[HabiticaItem], remote: Iterable[RemoteTask], tz: ZoneInfo, *,
               completed_aliases: Iterable[str] = (), done_keys: Iterable[str] = (),
               max_deletes: int = 15, max_delete_ratio: float = 0.6,
               allow_mass_delete: bool = False) -> Plan:
    plan = Plan()
    ours = {t.alias: t for t in remote if is_managed(t.alias)}
    wanted = {i.key: i for i in desired}
    completed, done = set(completed_aliases), set(done_keys)

    for item in desired:                                       # display order -> creates appear in order
        existing = ours.get(item.key)
        if existing is None:
            if item.kind == "todo" and item.key in completed:
                plan.completed_keys.append(item.key)           # you ticked it off in Habitica
            else:
                plan.creates.append(item)
        elif existing.type != item.kind:                       # e.g. a to-do became a daily
            plan.deletes.append(existing)
            plan.creates.append(item)
        else:
            changed = update_payload(item, existing, tz)
            if changed:
                plan.updates.append((existing, item, changed))

    for alias, task in ours.items():
        if alias in wanted:
            continue
        if alias in done and task.type == "todo":
            plan.scores.append(task)                           # finished via chat -> award it in Habitica
        else:
            plan.deletes.append(task)

    if plan.deletes and not allow_mass_delete:
        n, m = len(plan.deletes), len(ours)
        if not desired and m > 0:
            plan.blocked = f"refused to delete all {m} agent-managed tasks because nothing is desired (data problem?)"
        elif n > max_deletes or (m >= 6 and n / m > max_delete_ratio):
            plan.blocked = f"refused to delete {n} of {m} agent-managed tasks in one run (safety stop)"
        if plan.blocked:
            plan.deletes = []
    return plan


# ------------------------------------------------------------------- report
@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    scored: list[str] = field(default_factory=list)
    completed_on_habitica: list[str] = field(default_factory=list)
    moves: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    blocked: str | None = None
    stopped_early: bool = False
    remaining_writes: int = 0
    calls: int = 0
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deleted or self.scored or self.moves)

    def summary(self) -> str:
        """Counts only - safe for public CI logs (no task titles)."""
        parts = [f"created {len(self.created)}", f"updated {len(self.updated)}",
                 f"deleted {len(self.deleted)}", f"completed {len(self.scored)}",
                 f"moves {self.moves}", f"calls {self.calls}"]
        if self.completed_on_habitica:
            parts.append(f"ticked-off-in-habitica {len(self.completed_on_habitica)}")
        if self.failed:
            parts.append(f"FAILED {len(self.failed)}")
        if self.blocked:
            parts.append("BLOCKED: " + self.blocked)
        if self.stopped_early:
            parts.append(f"stopped early, {self.remaining_writes} write(s) left for the next run")
        if self.dry_run:
            parts.insert(0, "DRY RUN")
        return "; ".join(parts)


# ------------------------------------------------------------------ executor
def sync(client: HabiticaClient, desired: Sequence[HabiticaItem], tz: ZoneInfo, *,
         today: date | None = None, done_keys: Iterable[str] = (), budget: Budget | None = None,
         dry_run: bool = False, max_deletes: int = 15, allow_mass_delete: bool = False) -> SyncReport:
    """Bring Habitica in line with ``desired``. Raises only for bad credentials."""
    report = SyncReport(dry_run=dry_run)
    today = today or datetime.now(tz).date()
    if budget is not None:
        client.budget = budget
    start_calls = client.calls

    try:
        remote = client.get_tasks()
        ours = {t.alias for t in remote if is_managed(t.alias)}
        maybe_done = [i for i in desired if i.kind == "todo" and i.key not in ours]
        completed_aliases: set[str] = set()
        if maybe_done:                                          # one extra read, only when needed
            completed_aliases = {t.alias for t in client.get_tasks("completedTodos") if t.alias}
        plan = build_plan(desired, remote, tz, completed_aliases=completed_aliases, done_keys=done_keys,
                          max_deletes=max_deletes, allow_mass_delete=allow_mass_delete)
        report.blocked = plan.blocked
        report.completed_on_habitica = list(plan.completed_keys)
        if plan.blocked:
            log.warning("sync blocked: %s", plan.blocked)

        if dry_run:
            report.created = [i.key for i in plan.creates]
            report.updated = [t.alias or t.id for t, _, _ in plan.updates]
            report.deleted = [t.alias or t.id for t in plan.deletes]
            report.scored = [t.alias or t.id for t in plan.scores]
            report.calls = client.calls - start_calls
            return report

        id_by_key: dict[str, str] = {t.alias: t.id for t in remote if is_managed(t.alias)}
        _run_writes(client, plan, tz, today, report, id_by_key)
        if not report.stopped_early:
            _fix_order(client, desired, id_by_key, report)
    except BudgetExhausted:
        report.stopped_early = True
    report.calls = client.calls - start_calls
    return report


def _run_writes(client: HabiticaClient, plan: Plan, tz: ZoneInfo, today: date,
                report: SyncReport, id_by_key: dict[str, str]) -> None:
    ops: list[tuple[str, object]] = (
        [("score", t) for t in plan.scores] + [("delete", t) for t in plan.deletes]
        + [("create", i) for i in plan.creates] + [("update", u) for u in plan.updates])
    for n, (op, obj) in enumerate(ops):
        try:
            if op == "score":
                client.score_task(obj.id, "up")
                report.scored.append(obj.alias or obj.id)
                id_by_key.pop(obj.alias or "", None)
            elif op == "delete":
                try:
                    client.delete_task(obj.id)
                except HabiticaNotFound:
                    pass                                         # already gone
                report.deleted.append(obj.alias or obj.id)
                id_by_key.pop(obj.alias or "", None)
            elif op == "create":
                created = _with_habit_fallback(
                    obj, lambda p: client.create_task(p), create_payload(obj, tz, today))
                id_by_key[obj.key] = created.id
                report.created.append(obj.key)
            else:
                remote, item, changed = obj
                _with_habit_fallback(item, lambda p: client.update_task(remote.id, p), changed)
                report.updated.append(remote.alias or remote.id)
        except BudgetExhausted:
            report.stopped_early = True
            report.remaining_writes = len(ops) - n
            return
        except HabiticaAuthError:
            raise
        except HabiticaNotFound as exc:                          # task vanished: next run recreates it
            report.failed.append((f"{op}", f"not found: {exc}"))
        except HabiticaError as exc:
            report.failed.append((f"{op}", str(exc)))


def _with_habit_fallback(item: HabiticaItem, call, payload: dict):
    """Habits' counter-reset ('frequency') is the one field I could not verify against the docs;
    if Habitica rejects it, retry without it rather than failing the whole task."""
    try:
        return call(payload)
    except HabiticaBadRequest:
        if item.kind == "habit" and "frequency" in payload:
            return call({k: v for k, v in payload.items() if k != "frequency"})
        raise


def _fix_order(client: HabiticaClient, desired: Sequence[HabiticaItem], id_by_key: Mapping[str, str],
               report: SyncReport) -> None:
    """Put our tasks at the top of each Habitica list, in display order, with the fewest moves."""
    want: dict[str, list[str]] = {name: [] for name in TASK_LISTS.values()}
    for item in desired:
        task_id = id_by_key.get(item.key)
        if task_id:
            want[TASK_LISTS[item.kind]].append(task_id)
    if not any(want.values()):
        return
    order = client.get_tasks_order()                             # one read, after any creates
    for list_name, front in want.items():
        if not front:
            continue
        current = list(order.get(list_name, []))
        moves = stagnant = 0
        while moves < MAX_MOVES_PER_LIST:
            plan = plan_moves(current, front)
            if not plan:
                break
            task_id, position = plan[0]
            try:
                observed = client.move_task(task_id, position)
            except BudgetExhausted:
                report.stopped_early = True
                report.remaining_writes += len(plan)
                return
            except HabiticaNotFound:
                break
            except HabiticaAuthError:
                raise
            except HabiticaError as exc:
                report.failed.append(("move", str(exc)))
                break
            moves += 1
            report.moves += 1
            if observed:                                         # Habitica tells us the real order: trust it
                stagnant = stagnant + 1 if observed == current else 0
                current = observed
                if stagnant >= 2:
                    report.failed.append(("move", f"order of '{list_name}' did not change after moving"))
                    break
            else:
                current = apply_moves(current, [(task_id, position)])
