import pytest

from uniagent.notion import (SCHEMAS, NotionAuthError, NotionClient, NotionError, NotionNotFound,
                             discover, schema_properties)
from uniagent.notion_setup import parse_page_id, setup
from uniagent.config import Rules

from fake_habitica import FakeClock
from fake_notion import FakeNotion


def client(fake, clock=None, **kw):
    clock = clock or FakeClock()
    return NotionClient(fake.token, transport=fake, clock=clock.now, sleep=clock.sleep, **kw), clock


def test_version_and_auth_headers_are_sent():
    fake = FakeNotion()
    c, _ = client(fake)
    setup(c, "parent-page", Rules())                     # fake rejects wrong version / token
    assert fake.databases


def test_bad_token_raises_without_leaking_it():
    fake = FakeNotion()
    c = NotionClient("ntn_WRONG_TOKEN", transport=fake)
    with pytest.raises(NotionAuthError) as err:
        c.query("ds-0001")
    assert "ntn_WRONG_TOKEN" not in str(err.value)


def test_missing_token_is_reported_up_front():
    with pytest.raises(NotionAuthError):
        NotionClient.from_env({})


def test_unshared_page_gives_a_helpful_404():
    fake = FakeNotion()
    c, _ = client(fake)
    with pytest.raises(NotionNotFound, match="shared"):
        c.list_children("some-other-page")


def test_429_waits_for_retry_after():
    fake = FakeNotion()
    c, clock = client(fake)
    setup(c, "parent-page", Rules())
    fake.fail_next = [(429, {"Retry-After": "3"}, {"object": "error", "message": "rate limited"})]
    c.query(next(iter(fake.sources)))
    assert 3.0 in clock.sleeps


def test_reads_retry_server_errors_but_writes_do_not():
    fake = FakeNotion()
    c, _ = client(fake)
    setup(c, "parent-page", Rules())
    ds = next(iter(fake.sources))
    fake.fail_next = [(503, {}, {}), (502, {}, {})]
    assert c.query(ds) == []                                              # retried transparently
    fake.fail_next = [(503, {}, {"message": "unavailable"})]
    with pytest.raises(NotionError):
        c.create_page(ds, {})                                             # might have been saved: never auto-retried


def test_queries_follow_pagination():
    fake = FakeNotion(page_size=2)
    c, _ = client(fake)
    setup(c, "parent-page", Rules())
    ds = fake.databases[next(i for i, d in fake.databases.items() if d["title"] == "State")]["ds"]
    for i in range(5):
        c.create_page(ds, {"Key": {"title": [{"text": {"content": f"k{i}"}}]}})
    assert len(c.query(ds)) == 5


def test_requests_are_paced_for_notions_rate_limit():
    fake = FakeNotion()
    c, clock = client(fake, min_interval_s=0.35)
    setup(c, "parent-page", Rules())
    assert clock.sleeps and all(s <= 0.35 + 1e-9 for s in clock.sleeps)


# ------------------------------------------------------------------- setup
def test_setup_creates_every_database_once():
    fake = FakeNotion()
    c, _ = client(fake)
    first = setup(c, "parent-page", Rules())
    assert sorted(first["created"]) == sorted(SCHEMAS) and first["existing"] == []
    second = setup(c, "parent-page", Rules())
    assert second["created"] == [] and len(fake.databases) == len(SCHEMAS)


def test_discovery_finds_data_sources_by_title():
    fake = FakeNotion()
    c, _ = client(fake)
    setup(c, "parent-page", Rules())
    ws = discover(c, "parent-page")
    assert set(ws.ds) == set(SCHEMAS) and ws["Tasks"].startswith("ds-")


def test_discovery_reports_missing_databases():
    fake = FakeNotion()
    c, _ = client(fake)
    with pytest.raises(NotionError, match="setup"):
        discover(c, "parent-page")


def test_data_source_is_looked_up_when_create_does_not_return_it():
    fake = FakeNotion(create_returns_sources=False)
    c, _ = client(fake)
    assert len(setup(c, "parent-page", Rules())["created"]) == len(SCHEMAS)


def test_schema_covers_every_column_the_code_writes():
    assert {"Name", "Key", "Status", "Scheduled", "Agent note", "Daily time", "Habit +"} <= set(schema_properties("Tasks"))


@pytest.mark.parametrize("text,expected", [
    ("1f2e3d4c5b6a79881f2e3d4c5b6a7988", "1f2e3d4c5b6a79881f2e3d4c5b6a7988"),
    ("https://www.notion.so/My-Page-1f2e3d4c5b6a79881f2e3d4c5b6a7988?pvs=4", "1f2e3d4c5b6a79881f2e3d4c5b6a7988"),
    ("1f2e3d4c-5b6a-7988-1f2e-3d4c5b6a7988", "1f2e3d4c5b6a79881f2e3d4c5b6a7988"),
])
def test_page_ids_can_be_pasted_as_urls(text, expected):
    assert parse_page_id(text) == expected


def test_garbage_page_id_is_rejected():
    with pytest.raises(ValueError):
        parse_page_id("not a page")
