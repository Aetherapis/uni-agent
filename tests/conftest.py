from datetime import date, datetime, time

import pytest
from pathlib import Path

from uniagent.config import Rules
from uniagent.timetable import BusTrip, ClassSession, Timetable

# Mon 26 Oct 2026 is a Monday; "now" defaults to Sunday evening before it.
SUN = datetime(2026, 10, 25, 20, 0)
MON = datetime(2026, 10, 26, 7, 0)


@pytest.fixture
def rules() -> Rules:
    return Rules()          # defaults: Friday evenings protected, 180 task-min/day cap


@pytest.fixture
def tt() -> Timetable:
    return Timetable(
        sessions=[
            ClassSession("Calculus", "Lecture", 0, time(10, 0), time(11, 30), "B12"),
            ClassSession("Physics", "Lab", 1, time(13, 0), time(16, 0)),
        ],
        buses=[
            BusTrip("R12", "to_campus", time(8, 30), time(9, 10), (0, 1)),
            BusTrip("R12", "to_campus", time(11, 30), time(12, 10), (0, 1)),
            BusTrip("R12", "to_home", time(13, 45), time(14, 25), (0, 1)),
            BusTrip("R12", "to_home", time(16, 30), time(17, 10), (0, 1)),
        ],
        term_start=date(2026, 9, 20),
    )


# Tests run on a fictional timetable with the same shape as the real one, so they work in a public repo
# and never depend on private data. (The real file, seed/timetable.toml, is git-ignored.)
SEED_PATH = Path(__file__).resolve().parents[1] / "seed" / "timetable.example.toml"
