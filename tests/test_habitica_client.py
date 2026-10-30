import pytest

from uniagent.habitica import (Budget, BudgetExhausted, HabiticaAuthError, HabiticaBadRequest,
                               HabiticaClient, HabiticaNotFound, HabiticaRateLimited)

from fake_habitica import FakeClock, FakeHabitica


def client(fake: FakeHabitica, delay=30.0, **kw) -> HabiticaClient:
    return HabiticaClient(fake.user_id, fake.api_key, call_delay_s=delay, transport=fake,
                          clock=fake.clock.now, sleep=fake.clock.sleep, **kw)


def test_every_call_is_at_least_30_seconds_after_the_previous_one():
    fake = FakeHabitica()
    c = client(fake)
    c.get_tasks(); c.get_tasks_order(); c.get_tasks("completedTodos")
    times = [e[0] for e in fake.log]
    assert len(times) == 3 and all(b - a >= 30 for a, b in zip(times, times[1:]))


def test_slow_calls_do_not_add_extra_waiting():
    fake = FakeHabitica()
    c = client(fake)
    c.get_tasks()
    fake.clock.t += 100                      # plenty of time passed on its own
    c.get_tasks()
    assert fake.clock.sleeps == []


def test_x_client_header_and_credentials_are_sent():
    fake = FakeHabitica()
    client(fake, delay=0).get_tasks()
    headers = fake.log[0][3]
    assert headers["x-client"] == "user-1-uniagent"
    assert headers["x-api-user"] == "user-1" and headers["x-api-key"] == "secret-key"


def test_bad_credentials_raise_and_never_leak_the_key():
    fake = FakeHabitica()
    bad = HabiticaClient("user-1", "WRONG-KEY-123", call_delay_s=0, transport=fake,
                         clock=fake.clock.now, sleep=fake.clock.sleep)
    with pytest.raises(HabiticaAuthError) as err:
        bad.get_tasks()
    assert "WRONG-KEY-123" not in str(err.value)


def test_missing_credentials_are_reported_before_any_network_call():
    with pytest.raises(HabiticaAuthError):
        HabiticaClient.from_env({})


def test_from_env_reads_credentials_name_and_delay():
    fake = FakeHabitica()
    c = HabiticaClient.from_env({"HABITICA_USER_ID": "user-1", "HABITICA_API_KEY": "secret-key",
                                 "HABITICA_CLIENT_NAME": "mybot", "HABITICA_CALL_DELAY": "45"},
                                transport=fake, clock=fake.clock.now, sleep=fake.clock.sleep)
    c.get_tasks()
    assert fake.log[0][3]["x-client"] == "user-1-mybot" and c.call_delay_s == 45.0


def test_429_waits_for_retry_after_then_succeeds():
    fake = FakeHabitica()
    fake.fail_next = [(429, {"Retry-After": "7"}, {"success": False})]
    c = client(fake, delay=0)
    assert c.get_tasks() == []
    assert 7 in fake.clock.sleeps and len(fake.log) == 2


def test_gives_up_after_repeated_429():
    fake = FakeHabitica()
    fake.fail_next = [(429, {"Retry-After": "1"}, {})] * 10
    with pytest.raises(HabiticaRateLimited):
        client(fake, delay=0, max_retries=2).get_tasks()


def test_server_errors_are_retried_with_backoff():
    fake = FakeHabitica()
    fake.fail_next = [(503, {}, {}), (502, {}, {})]
    assert client(fake, delay=0).get_tasks() == []
    assert len(fake.log) == 3


def test_budget_stops_the_run_before_it_would_overrun():
    fake = FakeHabitica()
    c = client(fake, budget=Budget(50, fake.clock.now))
    c.get_tasks()                            # call 1 at t=0
    c.get_tasks_order()                      # call 2 after a 30 s pause (t=30)
    with pytest.raises(BudgetExhausted):     # call 3 would need to wait until t=60 > 50
        c.get_tasks()
    assert len(fake.log) == 2


def test_error_mapping():
    fake = FakeHabitica()
    c = client(fake, delay=0)
    with pytest.raises(HabiticaNotFound):
        c.delete_task("does-not-exist")
    c.create_task({"type": "todo", "text": "a", "alias": "t-a"})
    with pytest.raises(HabiticaBadRequest):
        c.create_task({"type": "todo", "text": "b", "alias": "t-a"})        # duplicate alias


def test_move_returns_the_observed_order():
    fake = FakeHabitica()
    c = client(fake, delay=0)
    a = c.create_task({"type": "todo", "text": "a", "alias": "t-a"})
    b = c.create_task({"type": "todo", "text": "b", "alias": "t-b"})        # new tasks go on top: [b, a]
    assert c.move_task(b.id, -1) == [a.id, b.id]
