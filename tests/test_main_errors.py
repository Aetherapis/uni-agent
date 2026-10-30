import pytest

from uniagent import runner
from uniagent.config import load_rules
from uniagent.gemini import GeminiBadRequest, GeminiError
from uniagent.habitica import HabiticaBadRequest
from uniagent.notion import NotionError, NotionNotFound

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(runner, "build_services", lambda env, rules, need_telegram=True: object())
    monkeypatch.delenv("UNIAGENT_DEBUG", raising=False)

    def make(outcome):
        def fake_run(svc, **kw):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        monkeypatch.setattr(runner, "run_once", fake_run)
    return make


def go(capsys, *args):
    code = runner.main(["--config", str(ROOT / "config.toml"), *args])
    return code, capsys.readouterr()


def test_a_normal_run_prints_the_counts_and_exits_zero(patched, capsys):
    patched(runner.RunReport(processed=2))
    code, out = go(capsys)
    assert code == 0 and "processed 2" in out.out


def test_unexpected_errors_exit_2_and_never_print_the_message(patched, capsys):
    patched(RuntimeError("my private task: surprise party for Sam"))
    code, out = go(capsys)
    assert code == 2 and "RuntimeError" in out.out and "surprise" not in out.out + out.err


def test_debug_mode_prints_the_traceback(patched, capsys, monkeypatch):
    patched(RuntimeError("details here"))
    code, out = go(capsys, "--debug")
    assert code == 2 and "details here" in out.err
    monkeypatch.setenv("UNIAGENT_DEBUG", "1")
    assert "details here" in go(capsys)[1].err


@pytest.mark.parametrize("exc", [NotionError("down", 503), NotionError("rate", 429), NotionError("network error"),
                                 GeminiError("overloaded", 500)])
def test_temporary_trouble_is_a_quiet_green_run(patched, capsys, exc):
    patched(exc)
    code, out = go(capsys)
    assert code == 0 and "Temporary problem" in out.out


@pytest.mark.parametrize("exc", [NotionNotFound("page missing", 404), HabiticaBadRequest("bad field", 400),
                                 GeminiBadRequest("bad request", 400), NotionError("bad payload", 400)])
def test_persistent_problems_turn_the_run_red(patched, capsys, exc):
    patched(exc)
    assert go(capsys)[0] == 2
