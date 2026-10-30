# uniagent - university task agent -> Habitica

Send tasks by text or voice note; the agent schedules them around your timetable and
bus times, protects you from burnout, and keeps your **Habitica to-do list sorted by
time** (title carries the time, e.g. `16:00-17:00 Lab report`; top of the list = next).

**Setting it up: see [SETUP.md](SETUP.md) for the complete step-by-step guide.**

## Status

| Part | What | State |
|------|------|-------|
| 1 | Core engine: timetable, buses, scheduler, burnout rules, vacation mode, to-do/daily/habit types, Habitica ordering | **done** (this zip, v1.1) |
| 2 | Habitica client + state-based sync (create/update/delete/complete/reorder, 30 s spacing, time budget, safety stops) | **done** |
| 3 | Notion storage (tasks, timetable, buses, vacations, inbox queue, state) + setup script | **done** |
| 4 | Gemini interpreter (text + voice -> validated commands, uni glossary) + command executor | **done** |
| 5 | Telegram intake + the run loop (ingest, interpret, plan, reply, sync) | **done** |
| 6 | GitHub Actions workflows (agent, keepalive), pre-flight doctor, hardened entry point | **done** |
| 7 | Polish: instant-reply relay, evening/morning digests, weekly review, rolling-window burnout guard | **done** |

## Try it (no accounts needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q                    # 405 tests
python -m uniagent.demo --now "2026-10-26 20:00"   # runs on YOUR timetable (seed/timetable.toml)
# config.toml is already set up for Egypt (Africa/Cairo) - edit limits to taste
```

## Your data

* `seed/timetable.toml` is YOUR private timetable (git-ignored; imported into Notion once, then edited in Notion).
  `seed/timetable.example.toml` is a fictional copy with the same shape; the tests run on it, so the
  repository can be public without exposing your rooms, groups or bus times.
* `config.toml` holds preferences only (timezone, limits, digest times): safe to commit.
* Biweekly classes use `every_weeks = 2` plus an `anchor` date on which the class happens; one-off
  cancellations go in the Exceptions database (or just tell the bot).

## Three kinds of tasks -> three Habitica lists

| You say | Type | Habitica | What the agent does |
|---|---|---|---|
| "submit the lab report by Thursday" | one-time | To-Do | finds a slot, sorts by date + time |
| "review lectures every night" / "German vocab every Sun & Tue" | daily | Daily | picks ONE stable time that fits every repeat day and reserves it, so it counts as workload |
| "drink more water" / "stop checking my phone" | habit | Habit | never scheduled; gets a + and/or - button |

`classify.py` is a transparent rule-based classifier with a confidence score (the agent can ask
when it is unsure: "twice a week" -> which days?). Part 4's Gemini does the real language
understanding; this is the fallback and a sanity check.

## Vacation mode

Tell the agent "away from Wednesday after uni until late Friday" (`[[away]]` in the data). Inside that
window only ~35% of the normal daily task cap is used (`away_load_factor`; "light" / "half" / "none"
or a number). It prefers doing work before you leave. A hard deadline that can only be met during the
break is still scheduled - but flagged, never silent.

## How it works (design in one screen)

* **The LLM understands you; plain code does the scheduling.** LLMs are unreliable at
  time arithmetic, so the scheduler (`scheduler.py`) is deterministic and unit-tested.
* **All datetimes are naive local time** in your timezone. Conversion happens only at the edges.
* **Stable plans.** A task that already has a valid slot is never moved when something else
  changes. Give a task a new date / time / deadline and only that task is re-planned.
* **Honest about limits.** If something can't fit before its deadline the agent says so
  (`warnings` / `unplaced`) instead of breaking a rule silently. Protected evenings and
  daily caps are only relaxed as a *flagged* last resort.
* **Burnout rules** (`config.toml`): daily task cap, total (classes + tasks) cap, buffers
  around everything, no tasks outside day hours, protected rest evenings/days, long tasks
  split into sessions, deadlines finished a day early when possible, load spread across days.
* **Habitica ordering** (`ordering.py`): Habitica to-dos only have a due *date*, so tasks
  are sorted by (date, time) and `plan_moves` computes the fewest "move to position"
  calls needed - your own Habitica tasks stay underneath, untouched.

## Polish (Part 7)

* **Rolling-window burnout guard:** in ANY 4-hour stretch at most 2.5 h of task time is planned
  (`rolling_window_min`, `max_rolling_task_min`), so a long free day gets real breaks even when the
  daily cap would allow more.
* **Digests:** an evening message previewing tomorrow (first bus, classes, tasks, dailies, deadlines in
  the next 3 days), an optional morning message, and a Friday weekly review (what you finished, how the
  next 7 days look, days that are nearly full). Each is sent once a day, only within 3 hours after its
  time, and skipped when there is nothing to say. `/digest` and `/review` show them on demand.
* **Instant replies:** `relay/worker.js` (Cloudflare Workers free plan) receives Telegram's webhook,
  stores the message in the Notion Inbox *first*, then wakes the GitHub workflow. If Notion fails it
  answers 500 so Telegram retries; if GitHub fails nothing is lost because scheduled runs still read the
  Inbox. The agent tolerates webhook mode (Telegram refuses polling while a webhook is set), and
  `python -m uniagent.relay_setup set|clear|info|inbox-id` switches modes. The Worker and the agent are
  tested against each other (the rows the Worker writes are read back by the agent).
* Not built, on purpose: learning your real task durations (would need time tracking) and Arabic replies.

## Deployment (Part 6)

* `agent.yml` runs on a schedule, on demand (with modes: normal / sync-only / force-sync / skip-habitica /
  doctor / doctor-with-ai) and on `repository_dispatch` (so an external webhook can wake it). One run at a
  time, never cancelled midway, read-only token, 40-minute cap, not run on forks.
* `keepalive.yml` makes one empty commit when the repo has been quiet for 40 days, because GitHub silently
  disables schedules in public repos after 60 days without activity.
* The entry point prints only exception *types* (a public log must never contain message text): network
  blips and rate limits end as a green run that retries next time; real problems turn the run red.
* The workflow files are tested against the code: every secret the code reads is passed, schedules respect
  GitHub's 5-minute minimum and leave no gap over an hour in either Cairo time zone, the keepalive script is
  run against a real git repository, and no step can print a secret.

## The run loop (Part 5)

* **Crash-safe order:** Telegram message -> Notion Inbox (Pending) -> offset saved -> interpreted ->
  commands applied -> Inbox marked Done. Inbox rows are de-duplicated by Telegram update id.
* **Cheap when idle:** no Gemini call without new messages; Habitica is only visited when the desired
  state changed (fingerprint) or every 6 h to catch manual edits. The reply is sent *before* the
  (30 s per call) Habitica sync, so you get feedback immediately.
* **Owner only:** messages from any other chat are ignored without a reply.
* **Resilience:** quota exhausted -> messages wait and you are told once per 3 h; a message that can't
  be understood is retried 3 times, then marked Error and you are told; a soft lock stops two overlapping
  runs from processing the same messages; sync safety stops are reported once and can be overridden
  with `/sync force`.
* **Proactive:** one "Heads up" message when the plan newly contains a problem (deadline that can't be met,
  protected time used, ...), not repeated until the set of problems changes.
* Commands that need no AI: `/help /agenda [today|tomorrow|N|date] /status /replan /sync [force]`.

## Interpreter (Part 4)

* **Gemini understands, code decides.** One call per batch of messages returns JSON (`add_task`,
  `update_task`, `complete_task`, `cancel_task`, `check_in`, `add_away`, `cancel_class`, `add_event`,
  `show_agenda`, `replan_all`) plus transcripts of voice notes. It never picks time slots.
* **Its output is treated as untrusted:** every field is parsed and range-checked, past dates rejected,
  references to existing tasks resolved against real data (ambiguous -> a question, never a guess),
  unsure task types -> a question, and a batch that cancels/completes many tasks is refused.
* The prompt carries a 14-day calendar (so "Thursday" is unambiguous), your class slots, course
  nicknames (math, DLD, EAP...), the active task list and student vocabulary.
* Free-tier quotas are per model: a 429 or unknown model moves on to the next model in `[gemini] models`.
  No Gemini call is made when there is nothing new to interpret.
* Writes are at-most-once: a command that fails is reported and skipped, never auto-retried.

## Notion storage (Part 3)

* Seven databases under one Notion page: **Tasks**, **Timetable**, **Buses**, **Away** (vacations),
  **Exceptions** (cancelled classes, one-off events), **Inbox** (the persistent pending queue of messages)
  and **State** (small values such as the Telegram offset). Found by title, so the only settings are the
  integration token and the parent page.
* Everything personal lives in Notion, not in the repo. A row you add or edit by hand in the Notion app is
  picked up on the next run; unusable rows are skipped and reported, never fatal.
* Writing the schedule back updates only pages whose slot / daily time / note actually changed, so an
  unchanged plan costs zero writes. Times are stored with the Cairo UTC offset (DST-safe).
* Writes are never auto-retried after a server error (could duplicate a page); reads are.

## Habitica sync (Part 2)

* **State-based:** each run diffs "what Habitica should contain" against what it contains and applies only
  the difference, so it is idempotent and a run cut short by its time budget just continues next time.
* **Only tasks with our alias (`t-...`, `c-...`) are ever changed or deleted.** Your own tasks stay below ours.
* Updates send only changed fields (streaks are never reset). Finished-in-chat to-dos are *completed* in
  Habitica (so you get the reward); finished-in-Habitica to-dos are reported back, never re-created.
* Safety stops: mass deletes and "nothing desired" are refused and reported. Logs contain counts only.
* Order is fixed per list with the fewest moves, re-planned from the order Habitica reports after each move.

## Running on GitHub Actions (the plan for Parts 5-6)

* Scheduled workflows run **at most every 5 minutes** and can be delayed, so there is no
  30-second loop. Instead each run: reads new Telegram messages (Telegram keeps them for
  24 h), processes the whole batch, then syncs to Habitica, spacing calls ~30 s apart.
* **Public repos get unlimited Actions minutes; private repos get a monthly quota**
  (about 3,000 min on GitHub Pro). A 5-minute cron in a private repo would use it up,
  so either keep the repo public (no personal data lives in it - that's in Notion, and
  logs only contain counts) or use a 15-minute cron in a private repo.
* Secrets (Habitica, Notion, Gemini, Telegram) go in **GitHub -> Settings -> Secrets**,
  never in files.

## Layout

```
src/uniagent/models.py      Task, Placement, Block
src/uniagent/config.py      Rules + config.toml loader (validates, catches typos)
src/uniagent/timetable.py   sessions (odd/even weeks, cancellations), bus -> commute blocks
src/uniagent/scheduler.py   the scheduler (todos, dailies, vacation) + split_task
src/uniagent/classify.py    todo / daily / habit classifier
src/uniagent/ordering.py    Habitica items, sort order, minimal reordering plan
src/uniagent/habitica.py    API client: 30 s spacing, X-Client, 429/Retry-After, run budget
src/uniagent/habitica_sync.py  diff desired vs Habitica -> create/update/delete/complete/move
src/uniagent/notion.py      Notion client (API 2026-03-11, data sources), database layout, discovery
src/uniagent/notion_store.py  tasks / timetable / vacations / inbox queue / state <-> Notion
src/uniagent/notion_setup.py  creates the databases and imports seed/timetable.toml
src/uniagent/telegram.py    Telegram Bot API client (owner-only, token never logged)
src/uniagent/replies.py     everything the bot says back (facts come from the scheduler, not the LLM)
src/uniagent/runner.py      ONE run: ingest -> process -> plan -> reply -> Habitica sync
src/uniagent/digest.py      evening/morning previews and the weekly review
src/uniagent/relay_setup.py switch between polling and the instant-reply relay
relay/worker.js             Cloudflare Worker: Telegram webhook -> Notion Inbox + GitHub wake (with its own tests)
src/uniagent/doctor.py      pre-flight check (python -m uniagent.doctor --ai --audio --ping)
.github/workflows/agent.yml      the scheduled run (5 min by day, hourly at night; serialised)
.github/workflows/keepalive.yml  stops GitHub disabling the schedule after 60 quiet days
.github/workflows/ci.yml         runs the tests on every push
src/uniagent/gemini.py      Gemini REST client: JSON-schema output, voice notes inline, model fallback chain
src/uniagent/interpreter.py prompt + context + STRICT validation of the model's output -> commands
src/uniagent/actions.py     applies commands (tasks, vacations, cancelled classes, events), at-most-once
src/uniagent/loaders.py     dict/TOML -> models (Notion rows later)
src/uniagent/render.py      text view of a schedule
src/uniagent/demo.py        runnable demo
seed/timetable.toml         your timetable + buses
config.toml                 your preferences
tests/                      405 tests incl. randomised invariants and bug-injection checks
```
