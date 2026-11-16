from datetime import datetime
from zoneinfo import ZoneInfo

from app.schedule import Schedule

TZ = ZoneInfo("Asia/Almaty")
WED = (2026, 11, 4)  # a Wednesday


def at(hhmm: str, day=WED) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(*day, h, m, tzinfo=TZ)


def sched(defaults, weekdays=None) -> Schedule:
    return Schedule.from_dict({"timezone": "Asia/Almaty", "defaults": {"windows": defaults}, "weekdays": weekdays or {}})


def test_unsorted_windows_never_skip_the_earlier_one():
    s = sched(["18:00-20:00", "09:00-10:00"])
    assert s.seconds_until_next_open(at("08:00")) == 3600
    assert s.is_open(at("09:30"))
    assert s.seconds_until_next_open(at("09:30")) == 0
    assert s.seconds_until_next_open(at("10:30")) == int(7.5 * 3600)


def test_explicit_empty_list_means_no_windows_missing_means_defaults():
    s = sched(["07:00-22:00"], {"wednesday": {"windows": []}, "thursday": {}})
    assert s.days[2].windows == []
    assert not s.is_open(at("12:00"))
    assert s.days[3].windows == s.default_windows


def test_overnight_window_counts_after_midnight():
    s = sched([], {"tuesday": {"windows": ["22:00-02:00"]}})
    assert s.is_open(at("01:00"))  # Wednesday 01:00 belongs to Tuesday's window
    assert s.seconds_until_close(at("01:00")) == 3600


def test_overlapping_windows_report_latest_close():
    s = sched(["09:00-10:00", "09:30-12:00"])
    assert s.seconds_until_close(at("09:45")) == int(2.25 * 3600)


def test_no_windows_at_all_rechecks_hourly():
    s = sched([])
    assert s.seconds_until_next_open(at("12:00")) == 3600


def test_roundtrip_keeps_defaults_inheritance():
    s = sched(["08:00-09:00"], {"friday": {"enabled": False}, "monday": {"windows": []}})
    d = s.to_dict()
    assert "windows" not in d["weekdays"]["tuesday"]
    assert d["weekdays"]["monday"]["windows"] == []
    assert Schedule.from_dict(d).to_dict() == d
