# University Task Agent - Complete Setup Guide

*Telegram -> Gemini -> Notion -> Habitica, running free on GitHub Actions. Written 29 Oct 2026 for the code in uniagent-complete.zip.*

## 1. What you are building

You send a text or voice note to a Telegram bot. The message is saved in a Notion Inbox, Gemini turns it into commands, a plain-code scheduler plans around your classes, buses and workload limits, the bot replies with the real times, and Habitica gets a sorted list of to-dos, dailies and habits.

A GitHub Actions job runs the agent every 5 minutes by day and hourly overnight. Everything uses free tiers. Plan on 60-90 minutes for the core setup and about 20 more for the optional instant-reply relay. Each step ends with a check, so do them in order and do not move on until the check passes.

## 2. Gather these 7 values (you will collect them in steps 2-5)

| Name | What it is | Comes from |
|---|---|---|
| TELEGRAM_BOT_TOKEN | the bot's password | step 2 |
| TELEGRAM_CHAT_ID | your numeric Telegram id | step 2 |
| NOTION_TOKEN | Notion integration secret | step 3 |
| NOTION_PARENT_PAGE | URL (or id) of the Notion page that will hold the databases | step 3 |
| HABITICA_USER_ID | your Habitica user id | step 4 |
| HABITICA_API_KEY | your Habitica API Token (not your password) | step 4 |
| GEMINI_API_KEY | Google AI Studio key | step 5 |

Keep them in a password manager or a private note until step 8. Never paste them into a chat, a screenshot, a file in the repo, or an issue.

## 3. Rules that keep you safe

- Tokens only ever go into GitHub Secrets (step 8) or a terminal session. Never into code, config.toml, or commits.
- Your real timetable is in seed/timetable.toml. It is git-ignored on purpose, so it is imported into Notion once and never published. seed/timetable.example.toml is a fictional copy used by the tests.
- A public repository shows the code, config.toml (timezone, limits, digest times) and the run summaries (counts only, never task text). Public repos get unlimited free Actions minutes. A private repo works too but needs a slower schedule (see section 17).
- Only your own Telegram chat is obeyed. Anyone else who finds the bot is ignored silently.
- Free Gemini usage may be used by Google to improve its products under its free-tier terms. Your task text and voice notes go through it. Check the current terms if that matters to you.

## 4. Step 1 - Create the GitHub repository

1. On github.com click New repository. Name it for example uni-agent.
2. Choose Public (recommended, unlimited minutes) or Private.
3. Tick Add a README file. This creates the main branch, which Codespaces needs.
4. Create the repository.

Check: you see the repo page with a README.

## 5. Step 2 - Telegram bot and your chat id

1. In Telegram open the verified account @BotFather and send /newbot.
2. Choose a display name, then a username ending in bot. BotFather replies with a token that looks like 123456789:AAH... Save it as TELEGRAM_BOT_TOKEN.
3. Open your new bot and send /start (any message works).
4. In a browser open https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates with your token pasted in place of <YOUR_TOKEN>.
5. In the JSON find "chat":{"id":123456789 and copy that number as TELEGRAM_CHAT_ID. If the list is empty, send the bot another message and reload.

Optional: send /setcommands to BotFather, pick your bot, and paste these lines to get a command menu:

```
agenda - what is planned (today, tomorrow, 3)
digest - tomorrow's plan
review - weekly review
status - summary
replan - rebuild the plan
sync - sync Habitica now
help - examples
```

Check: the getUpdates page showed your message and you saved a numeric id. Do this before enabling the optional relay (section 14), because getUpdates stops working once a webhook is set.

## 6. Step 3 - Notion integration and parent page

You must own the Notion workspace to create an integration. If your education workspace belongs to the university, use a personal workspace instead.

1. Open notion.so/profile/integrations and click New integration (newer workspaces call these Connections).
2. Name it, pick your workspace, type Internal, save.
3. Under Capabilities tick Read content, Update content and Insert content.
4. Under Configuration copy the Internal Integration Secret (starts with ntn_ or secret_). That is NOTION_TOKEN.
5. In Notion create a new empty page called University Agent.
6. On that page open the ... menu (top right), choose Connections (older versions: Add connections), and select your integration. Sharing a page also shares everything inside it.
7. Copy the page link (... menu -> Copy link). That URL is NOTION_PARENT_PAGE. A bare 32-character id works too.

Check: the page's Connections list shows your integration.

## 7. Step 4 - Habitica credentials

1. Log in at habitica.com in a browser.
2. Open the user menu (top right) -> Settings -> API.
3. Copy User ID (HABITICA_USER_ID) and click to show the API Token (HABITICA_API_KEY). Never use your password here.
4. Still in Settings, check your time zone and Custom Day Start are what you expect. Habitica due dates are whole days, so a day start of 00:00 is simplest.

The agent only touches tasks whose internal alias starts with t- or c-. Your own Habitica tasks are never changed and stay below the agent's tasks in each list.

Check: you have a user id (a long UUID) and an API token.

## 8. Step 5 - Gemini API key

1. Open aistudio.google.com/apikey and sign in with a Google account.
2. Click Create API key (choose or create a project) and copy it as GEMINI_API_KEY.

A Gemini Plus or Pro subscription does not include API quota: the API free tier is separate and needs no card. The key works only if the model names in config.toml exist for your account. The defaults are gemini-3.5-flash, gemini-3.5-flash-lite and gemini-flash-latest. If a name is rejected the agent tries the next one, and the doctor in step 9 tells you which model answered.

## 9. Step 6 - Put the code in the repository (with Codespaces)

Codespaces runs a ready terminal in your browser. GitHub Pro includes plenty of free hours, and you only need it for setup.

1. On the repo page click Code -> Codespaces -> Create codespace on main.
2. Drag uniagent-complete.zip into the file explorer on the left.
3. In the terminal run:

```
unzip uniagent-complete.zip
cp -a uniagent/. .
rm -rf uniagent uniagent-complete.zip
git check-ignore -v seed/timetable.toml
git add -A
git status --short | grep seed
```

The check-ignore line must print a .gitignore rule, and the last line must list only seed/timetable.example.toml. If seed/timetable.toml appears in the list, stop and do not commit.

4. Commit and push:

```
git commit -m 'Add the university agent'
git push
```

If the push is rejected with a message about the workflow scope, run: unset GITHUB_TOKEN && gh auth login --scopes workflow, finish the browser prompt, then git push again.

Keep this Codespace open for step 7. Your private seed/timetable.toml lives only here.

Check: the repo page now shows .github, src, tests, relay, seed and config.toml, and the Actions tab lists the workflows agent, keepalive and tests.

## 10. Step 7 - Create the Notion databases and import your timetable

Still in the Codespace terminal:

```
export PYTHONPATH=src
read -s -p 'Notion token: ' NOTION_TOKEN; export NOTION_TOKEN
python -m uniagent.notion_setup --parent 'PASTE-THE-PAGE-URL-HERE' --seed seed/timetable.toml
```

Paste the token when prompted (it is not echoed). Expected output:

```
created: Tasks, Timetable, Buses, Away, Exceptions, Inbox, State; already there: nothing; timetable imported: True
```

Open the University Agent page in Notion. You should see seven databases. Timetable should have 16 rows, Buses 4 rows, and State a row named periods.

Then check the bus data in Notion. You confirmed that return buses leave campus at 15:30 and 17:30 and reach the dorms at 16:30 and 18:30. The morning buses leave the dorms at 06:40 and 08:40, but their campus arrival times (07:40 and 09:40) are an assumption of a 60 minute ride. If the real ride is longer, edit the Arrive column of those two Buses rows.

The script is safe to repeat: it finds databases by title, never duplicates them, and only imports the timetable into an empty table. From now on edit your timetable in Notion, not in the file.

## 11. Step 8 - Add the 7 secrets to GitHub

1. Repo -> Settings -> Secrets and variables -> Actions -> New repository secret.
2. Add each name from the table in section 2, exactly as written (capital letters, underscores), with its value:

| Secret | Value to paste |
|---|---|
| TELEGRAM_BOT_TOKEN | the full token from BotFather |
| TELEGRAM_CHAT_ID | digits only, e.g. 123456789 |
| NOTION_TOKEN | ntn_... or secret_... |
| NOTION_PARENT_PAGE | the page URL |
| HABITICA_USER_ID | the user id |
| HABITICA_API_KEY | the API token |
| GEMINI_API_KEY | the AI Studio key |

Secrets cannot be read back later. If one is wrong, add it again with the same name to overwrite it.

Check: the Secrets page lists all 7 names.

## 12. Step 9 - Run the doctor

The doctor tests every connection without changing your data (it writes and trashes one scratch row in Notion to prove write access, and sends one Telegram message).

1. Repo -> Actions -> agent (left list) -> Run workflow.
2. Choose Mode: doctor-with-ai and run it. Open the run and read the Summary (or the log of the step Run the agent).

A healthy result looks like this:

```
[OK] settings: all 7 secrets are set
[OK] config: timezone Africa/Cairo, 150 min/day cap
[OK] notion: 7 databases, 16 classes, 4 bus trips, 0 active tasks, 0 unusable row(s), write access ok
[OK] timetable: classes loaded
[OK] habitica: credentials ok, 12 tasks (0 managed by the agent)
[OK] telegram: bot @your_bot ok, messages are polled (no webhook), test message sent
[OK] gemini: key ok, model gemini-3.5-flash answered in JSON
[OK] gemini-audio: voice notes in Telegram's format (audio/ogg) are accepted by gemini-3.5-flash
```

You should also receive the message Doctor check: Telegram works. in Telegram.

If a line says FAIL:

| Line | Likely cause and fix |
|---|---|
| settings: missing: NAME | that secret is missing or misspelled (step 8) |
| notion: NotionNotFound ... shared | the page is not shared with the integration (step 3, point 6), or NOTION_PARENT_PAGE is wrong |
| notion: NotionAuthError | wrong or revoked NOTION_TOKEN |
| notion: ... missing under the parent page | run the setup script (step 7) |
| timetable: empty | the setup ran without --seed; the Timetable database has no rows |
| habitica: HabiticaAuthError | wrong user id or you used your password instead of the API Token |
| telegram: TelegramAuthError | wrong bot token |
| telegram: TELEGRAM_CHAT_ID must be a number | you pasted a username, not the numeric id |
| gemini: GeminiAuthError | wrong key, or the key was created in a restricted project |
| gemini: GeminiQuotaExhausted | free quota used for today; try again later or later in the day |
| gemini: ... not available | all model names rejected; edit [gemini] models in config.toml |
| gemini-audio: FAIL | Gemini rejected Telegram's voice format. Typed messages still work; voice notes will be reported as unreadable |

Fix, re-run the doctor, and continue only when everything except possibly gemini-audio is OK.

## 13. Step 10 - First real runs (staged)

Stage A: no Habitica yet.
1. Actions -> agent -> Run workflow -> Mode: skip-habitica.
2. In Telegram send /start. After the run you get the help text.
3. Send: remind me to email my TA tomorrow. Trigger the workflow again (or wait for the schedule). You should get a reply with a real time slot.
4. Open the Tasks database in Notion: the row has a Scheduled time and perhaps an Agent note.
5. Send a voice note, trigger a run, and check the reply and the transcript saved in the Inbox row.
6. Send /agenda tomorrow and /status.

Stage B: turn Habitica on.
1. Actions -> agent -> Run workflow -> Mode: force-sync (or just wait for the normal schedule).
2. In Habitica, check To-Dos: titles look like 16:00-17:00 Lab report, soonest at the top, your own tasks below. Dailies and Habits appear in their own lists.

Habitica asks automated tools to wait 30 seconds between calls, so a first sync of about 10 tasks takes around 7 minutes. That is normal. The sync stops after 25 minutes and the next run continues where it left off.

Stage C: let it run. Scheduled runs start from the main branch, and GitHub can start them 5-30 minutes late, especially at busy times. If nothing runs after an hour, see the troubleshooting table.

After that day-to-day behaviour is automatic. Idle runs are short (usually well under a minute) and do not call Gemini or Habitica unless something changed.

## 14. Optional - Instant replies (the relay)

Without the relay a message waits for the next scheduled run. With it, Telegram sends each message straight to a free Cloudflare Worker, which saves it in the Notion Inbox and wakes the GitHub workflow, so replies arrive within about a minute. If the relay or GitHub ever fails, nothing is lost because the scheduled runs still process the Inbox.

1. Get the Inbox id. In the Codespace terminal (with PYTHONPATH and NOTION_TOKEN set as in step 7) run:

```
export NOTION_PARENT_PAGE='PASTE-THE-PAGE-URL-HERE'
python -m uniagent.relay_setup inbox-id
```

It prints NOTION_INBOX_DS=... Copy the value.

2. Create a GitHub token for the Worker: GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate. Repository access: only your uni-agent repo. Repository permissions: Contents = Read and write (Metadata read is added automatically). Set the longest expiry and note the date. Copy the token.

3. Make a webhook secret: python3 -c 'import secrets; print(secrets.token_urlsafe(32))' and keep the output.

4. Create the Worker: sign up at cloudflare.com (free). Workers & Pages -> Create -> Create Worker -> Deploy. Then Edit code, delete the sample, paste the whole of relay/dist/worker.bundle.js from your repo, and Deploy.

5. In the Worker -> Settings -> Variables and Secrets add:

| Name | Type | Value |
|---|---|---|
| OWNER_CHAT_ID | text | your TELEGRAM_CHAT_ID |
| NOTION_INBOX_DS | text | the value from step 1 |
| GITHUB_REPO | text | yourname/uni-agent |
| GITHUB_EVENT | text | wake |
| WEBHOOK_SECRET | secret | the secret from step 3 |
| NOTION_TOKEN | secret | same Notion token |
| GITHUB_TOKEN | secret | the fine-grained token from step 2 |

Deploy again after saving. Note the Worker address, like https://uniagent-relay.yourname.workers.dev.

6. Point Telegram at it (Codespace terminal):

```
read -s -p 'Bot token: ' TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN
python -m uniagent.relay_setup set --url https://uniagent-relay.yourname.workers.dev --secret 'PASTE-THE-SECRET'
python -m uniagent.relay_setup info
```

7. Send the bot a message. The reply should arrive within about a minute. Run the doctor again: the telegram line should now say messages arrive via webhook.

To go back to polling at any time: python -m uniagent.relay_setup clear. If your GitHub token expires the instant wake stops working but everything still runs on the schedule. Make a new token and update the Worker secret GITHUB_TOKEN.

## 15. Using it day to day

Talk normally, in English, by text or voice. Examples:

- remind me to email the TA tomorrow
- physics lab report due Tuesday 5pm, about 4 hours (it splits long work into 90 minute sessions and finishes a day early when it can)
- revise German every Sunday and Tuesday (a daily)
- drink more water (a habit) and I drank water (logs a +1)
- move the report to Friday, push that to Thursday, I finished the quiz prep, cancel the notebook
- I am away from Wednesday after uni until late Friday (workload drops to about 35%)
- no physics lecture on the 8th, I have a math midterm on 3 Nov 10-12

Commands: /agenda (today), /agenda tomorrow, /agenda 3, /digest, /review, /status, /replan, /sync, /sync force, /help.

The bot sends an evening preview at 21:00 and a Friday review at 10:00 by itself. If it is unsure it asks one short question instead of guessing. It also sends a Heads up message when a deadline cannot be met or protected time had to be used.

Editing in Notion:
- You may add or edit rows in Tasks (title, deadline, status, minutes). Setting Status to Done completes the task in Habitica and awards the reward; Cancelled removes it.
- Do not edit the Scheduled and Agent note columns, they are rewritten by the agent.
- Edit Timetable, Buses, Away and Exceptions to change classes, buses, trips and cancellations.

Editing in Habitica:
- Tick tasks off normally; the agent notices within a few hours (or on the next /sync).
- Do not rename or delete the agent's tasks. If you delete one it is recreated; to remove a task, tell the bot or set it to Cancelled in Notion.

## 16. Settings you may want to change

Edit config.toml directly on GitHub (pencil icon, then Commit). The most useful keys:

| Key | Meaning |
|---|---|
| timezone | Africa/Cairo (handles the daylight saving change) |
| [day] start, end | earliest start and latest end of planned tasks |
| rest_evenings | evenings kept free (default Friday after 18:00) |
| [limits] max_task_minutes_per_day, weekday_task_caps | daily task budget and per-day overrides |
| away_load_factor | share of normal workload while you are away (0.35) |
| [pacing] rolling_window_min, max_rolling_task_min | at most 2.5 h of tasks in any 4 h stretch |
| max_chunk_min | long tasks are split into sessions of this length |
| [digest] morning, evening, weekly_day, weekly_time | automatic messages (use an empty string to switch one off) |
| [gemini] models | model fallback order |

## 17. Keeping it healthy

- Failures: a red run emails you. Real problems (wrong key, missing page) show a short error type in the log. Temporary network or rate-limit trouble ends green and retries next run.
- The keepalive workflow adds an empty commit after 40 quiet days, because GitHub switches off scheduled workflows in public repos after 60 days without activity. Check the Actions tab now and then.
- Private repo? The free quota is about 3,000 minutes a month, so edit .github/workflows/agent.yml and use a slower schedule (roughly 2,000 minutes a month; watch Settings -> Billing the first month):

```
- cron: '*/20 3-22 * * *'
- cron: '0 23,0-2 * * *'
```

- Updating the code: upload a new zip in a Codespace the same way as step 6, check that seed/timetable.toml is still ignored, commit and push. Your Notion data and config.toml are untouched unless you overwrite config.toml.
- Gemini free quotas and model names change over time. If replies stop with a quota notice, wait for the daily reset or edit [gemini] models.
- Turn on the debug mode of the manual run only in a private repo, because tracebacks can reveal more than counts.

## 18. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No scheduled runs appear | GitHub may start them late; also check that workflows are enabled and that the files are on the main branch. Run the agent manually once. |
| Bot never answers | Run the doctor. Check TELEGRAM_CHAT_ID. Make sure you are writing to your own bot from the account whose id you saved. |
| The reply says my voice note could not be processed | Run doctor-with-ai and read the gemini-audio line. Send it as text meanwhile. |
| I was told I am out of quota | Your messages are saved. They are processed automatically after the quota resets. |
| A task has no slot | Read its Agent note in Notion. Usually the deadline is too close, the day is full, or you are away. Extend the deadline or say so. |
| Habitica order looks wrong | Run mode force-sync. Make sure nothing renamed the agent's tasks. |
| Habitica sync paused message | The safety stop refused a large deletion. If intended send /sync force. |
| A class or bus is wrong | Fix the row in Notion (Timetable, Buses) or say no physics lecture on the 8th. Then send /replan. |
| Tasks appear at odd hours | Check the [day] start and end and your timezone in config.toml. |
| Notion errors after you moved or unshared the page | Re-share the page with the integration and re-check NOTION_PARENT_PAGE. |

## 19. If a key leaks

Revoke it at once, then update the GitHub secret:

- Telegram: BotFather -> /revoke, choose the bot, then use the new token.
- Notion: integration page -> regenerate the secret.
- Habitica: Settings -> API -> look for a reset or regenerate option for the token. If you cannot find one, change your password and contact Habitica support.
- Gemini: AI Studio -> delete the key and create another.
- GitHub token for the relay: delete it under Developer settings and make a new one.

## 20. Quick reference

| Task | Where |
|---|---|
| Run a check | Actions -> agent -> Run workflow -> doctor or doctor-with-ai |
| Run now | Actions -> agent -> Run workflow -> normal |
| Re-plan and sync only | mode sync-only or force-sync |
| Test without Habitica | mode skip-habitica |
| Setup databases | python -m uniagent.notion_setup --parent URL --seed seed/timetable.toml |
| Relay on, info, off | python -m uniagent.relay_setup set, info, clear |
| Run tests | python -m pytest -q |

Nothing here costs money. The pieces: GitHub Actions (free for public repos), Telegram, Notion, Habitica, Google AI Studio free tier, and optionally the Cloudflare Workers free plan.
