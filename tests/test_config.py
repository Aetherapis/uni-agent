import pytest

from uniagent.config import ConfigError, Rules, load_rules, rules_from_dict


def test_defaults_are_valid():
    assert Rules().task_cap(0) == 180


def test_example_config_loads(tmp_path):
    import pathlib
    example = pathlib.Path(__file__).resolve().parents[1] / "config.example.toml"
    rules = load_rules(example)
    assert rules.rest_evenings == (4,)
    assert rules.max_task_minutes_per_day == 180


def test_weekday_cap_override():
    rules = rules_from_dict({"limits": {"weekday_task_caps": {"sat": 60}}})
    assert rules.task_cap(5) == 60 and rules.task_cap(0) == 180


def test_typos_are_reported():
    with pytest.raises(ConfigError, match="typo"):
        rules_from_dict({"limits": {"max_task_minuts_per_day": 100}})
    with pytest.raises(ConfigError):
        rules_from_dict({"nonsense": {}})


def test_bad_values_are_reported():
    with pytest.raises(ConfigError):
        rules_from_dict({"timezone": "Mars/Base"})
    with pytest.raises(ConfigError):
        rules_from_dict({"day": {"start": "22:00", "end": "08:00"}})
