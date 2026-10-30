"""The GitHub Actions files are code too: check them against the rest of the project."""
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from uniagent.config import load_rules
from uniagent.doctor import OPTIONAL_ENV, REQUIRED_ENV

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
RULES = load_rules(ROOT / "config.toml")


def load(name):
    return yaml.safe_load((WF / name).read_text())


def triggers(wf):
    return wf.get("on") or wf.get(True)             # YAML turns the bare key `on` into True


def expand(field, lo, hi):
    out = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        out |= set(range(a, b + 1, step))
    return out


def runs_per_day(cron):
    minute, hour, dom, month, dow = cron.split()
    assert (dom, month, dow) == ("*", "*", "*")
    return sorted((h, m) for h in expand(hour, 0, 23) for m in expand(minute, 0, 59))


def agent_runs():
    return sorted({t for c in triggers(load("agent.yml"))["schedule"] for t in runs_per_day(c["cron"])})


# -------------------------------------------------------------- agent workflow
def test_agent_only_runs_on_schedule_manual_or_dispatch():
    assert set(triggers(load("agent.yml"))) == {"schedule", "workflow_dispatch", "repository_dispatch"}


def test_schedules_never_fire_more_often_than_every_five_minutes():
    times = [h * 60 + m for h, m in agent_runs()]
    gaps = [b - a for a, b in zip(times, times[1:] + [times[0] + 1440])]
    assert min(gaps) >= 5 and max(gaps) <= 60                      # GitHub's minimum; and never silent for > 1 h


def test_the_two_schedules_do_not_overlap():
    a, b = (set(runs_per_day(c["cron"])) for c in triggers(load("agent.yml"))["schedule"])
    assert not a & b


@pytest.mark.parametrize("day", [date(2026, 7, 15), date(2026, 12, 15)])         # Cairo summer time and winter time
def test_every_local_hour_has_a_run_and_the_daytime_is_every_five_minutes(day):
    tz = ZoneInfo("Africa/Cairo")
    per_local_hour = {h: 0 for h in range(24)}
    for h, m in agent_runs():
        utc = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
        per_local_hour[utc.astimezone(tz).hour] += 1
    assert all(n >= 1 for n in per_local_hour.values())
    assert all(per_local_hour[h] == 12 for h in range(6, 24))        # 06:00-23:59 local: every 5 minutes


def test_runs_are_serialised_and_never_cancelled_midway():
    wf = load("agent.yml")
    assert wf["concurrency"]["group"] and wf["concurrency"]["cancel-in-progress"] is False


def test_the_job_has_minimal_permissions_and_a_timeout_longer_than_the_habitica_budget():
    wf = load("agent.yml")
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["run"]["timeout-minutes"] >= RULES.habitica_budget_min + 10


def test_forks_do_not_run_the_agent():
    assert "fork" in load("agent.yml")["jobs"]["run"]["if"]


def test_actions_are_pinned_to_major_versions():
    for name in ("agent.yml", "keepalive.yml", "ci.yml"):
        for job in load(name)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w./-]+@v\d+", step["uses"]), step["uses"]


def run_step(wf_name="agent.yml"):
    return next(s for s in load(wf_name)["jobs"]["run"]["steps"] if s.get("name") == "Run the agent")


def test_every_secret_the_code_needs_is_passed_with_the_same_name():
    env = run_step()["env"]
    for name in REQUIRED_ENV:
        assert env[name] == "${{ secrets." + name + " }}", name


def test_every_environment_variable_the_code_reads_is_known():
    names = set()
    for path in (ROOT / "src" / "uniagent").glob("*.py"):
        names |= set(re.findall(r'(?:env|environ)\.get\(\s*"([A-Z_]+)"', path.read_text()))
    assert names <= set(REQUIRED_ENV) | set(OPTIONAL_ENV), names - set(REQUIRED_ENV) - set(OPTIONAL_ENV)


def test_no_step_can_print_secrets():
    for name in ("agent.yml", "keepalive.yml"):
        for job in load(name)["jobs"].values():
            for step in job["steps"]:
                script = step.get("run", "")
                assert not re.search(r"\bprintenv\b|\benv\b\s*(\||$)|set -x|echo .*\$\{?\w*(TOKEN|KEY)", script), script
    assert "pull_request_target" not in (WF / "agent.yml").read_text()


def test_the_run_step_uses_pipefail_and_never_interpolates_inputs_into_the_script():
    step = run_step()
    assert step["shell"] == "bash"                                    # bash -eo pipefail: a crash fails the job
    assert "${{" not in step["run"]                                   # inputs arrive via env, not string pasting


def test_every_mode_maps_to_real_command_line_flags():
    wf = load("agent.yml")
    modes = wf["on" if "on" in wf else True]["workflow_dispatch"]["inputs"]["mode"]["options"]
    script = run_step()["run"]
    for mode in modes:
        assert re.search(rf"^\s*{re.escape(mode)}\)", script, re.M), mode
    flags = set(re.findall(r"--[a-z-]+", script))
    sources = (ROOT / "src/uniagent/runner.py").read_text() + (ROOT / "src/uniagent/doctor.py").read_text()
    for flag in flags:
        assert f'"{flag}"' in sources, flag
    modules = set(re.findall(r"python -m (uniagent\.\w+)", script))
    assert modules == {"uniagent.runner", "uniagent.doctor"}
    for module in modules:
        assert (ROOT / "src" / (module.replace(".", "/") + ".py")).exists()


def test_the_runner_is_started_with_the_source_folder_on_the_path():
    assert run_step()["env"]["PYTHONPATH"] == "src"


# ------------------------------------------------------------------- keepalive
def keepalive_script():
    return next(s for s in load("keepalive.yml")["jobs"]["keepalive"]["steps"] if "run" in s)["run"]


def test_keepalive_runs_weekly_with_write_access_only_for_that_job():
    wf = load("keepalive.yml")
    assert wf["permissions"] == {"contents": "write"} and len(triggers(wf)["schedule"]) == 1
    minute, hour, dom, month, dow = triggers(wf)["schedule"][0]["cron"].split()
    assert (dom, month) == ("*", "*") and dow.isdigit() and minute.isdigit() and hour.isdigit()    # once a week


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("age_days,expect_commit", [(50, True), (10, False)])
def test_keepalive_adds_a_commit_only_when_the_repository_is_quiet(tmp_path, age_days, expect_commit):
    def git(*args, cwd, **kw):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout

    remote, work = tmp_path / "remote.git", tmp_path / "work"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("clone", str(remote), str(work), cwd=tmp_path)
    git("config", "user.name", "t", cwd=work); git("config", "user.email", "t@t", cwd=work)
    old = int(datetime.now(timezone.utc).timestamp()) - age_days * 86400
    env = {"GIT_AUTHOR_DATE": f"{old} +0000", "GIT_COMMITTER_DATE": f"{old} +0000", "PATH": "/usr/bin:/bin"}
    git("commit", "--allow-empty", "-m", "first", cwd=work, env=env)
    git("push", "origin", "HEAD:main", cwd=work)
    before = git("rev-list", "--count", "HEAD", cwd=work).strip()
    subprocess.run(["bash", "-eo", "pipefail", "-c", keepalive_script()], cwd=work, check=True, capture_output=True)
    after = git("rev-list", "--count", "HEAD", cwd=work).strip()
    assert (int(after) == int(before) + 1) is expect_commit
    assert git("rev-list", "--count", "main", cwd=remote).strip() == after            # and it was pushed
