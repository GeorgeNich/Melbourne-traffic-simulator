"""sim/conditional_speeds.py: maxspeed:conditional parsing and time windows."""

from __future__ import annotations

from datetime import datetime

import pytest

from sim.conditional_speeds import active, parse

SCHOOL = "40 @ (Mo-Fr 08:00-09:30, 14:30-16:00; PH off; SH off)"
EVENING = "40 @ (08:00-24:00)"


def test_parse_school_zone():
    c = parse(SCHOOL)
    assert c.kmh == 40
    assert len(c.windows) == 2 and c.windows[0].days == frozenset(range(5))
    assert (c.windows[0].start_min, c.windows[0].end_min) == (480, 570)
    assert c.off_on_public_holidays and c.off_on_school_holidays


def test_active_windows():
    monday = datetime(2026, 10, 26)
    assert active(SCHOOL, monday.replace(hour=8, minute=30))
    assert not active(SCHOOL, monday.replace(hour=7, minute=59))
    assert not active(SCHOOL, monday.replace(hour=9, minute=30))
    assert active(SCHOOL, monday.replace(hour=15))
    assert not active(SCHOOL, datetime(2026, 10, 31, 8, 30))  # Saturday
    assert active(EVENING, datetime(2026, 10, 31, 23, 59))
    assert not active(EVENING, monday.replace(hour=7))


def test_unsupported_rules_are_not_guessed():
    with pytest.raises(ValueError):
        parse("40 @ (wet)")
    with pytest.raises(ValueError):
        parse("none @ (Mo-Fr 08:00-09:00)")
