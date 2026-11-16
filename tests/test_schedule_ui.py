import threading

import pytest

from app import schedule_ui as ui
from app.store import BotStore

WED = 2


@pytest.fixture
def store(tmp_path):
    return BotStore(str(tmp_path / "state.json"))


def cb(store, data):
    return ui.handle_callback(data, store, threading.Event())


def windows(store, t=WED):
    s = store.snapshot().schedule
    ws = s.default_windows if t == ui.DEFAULTS_TARGET else s.days[t].windows
    return [f"{a:%H:%M}-{b:%H:%M}" for a, b in ws]


def add(store, t, start, end):
    (H, M), (h, m) = (map(int, start.split(":")), map(int, end.split(":")))
    return cb(store, f"sch:w:{t}:em:{H}:{M}:{h}:{m}")


def test_add_sorts_and_dedupes(store):
    add(store, WED, "18:00", "20:00")
    add(store, WED, "09:00", "10:00")
    add(store, WED, "09:00", "10:00")
    assert windows(store) == ["07:00-22:00", "09:00-10:00", "18:00-20:00"]


def test_same_start_and_end_is_rejected(store):
    text, _ = add(store, WED, "09:00", "09:00")
    assert "совпадают" in text
    assert windows(store) == ["07:00-22:00"]


def test_delete_by_value_survives_reordering(store):
    add(store, WED, "18:00", "20:00")
    # This button was rendered for 18:00-20:00; adding an earlier window
    # shifts positions, but the button must still delete 18:00-20:00.
    add(store, WED, "09:00", "10:00")
    cb(store, "sch:x:2:18002000")
    assert windows(store) == ["07:00-22:00", "09:00-10:00"]


def test_stale_delete_is_a_noop_with_a_note(store):
    text, _ = cb(store, "sch:x:2:18002000")
    assert "уже нет" in text
    assert windows(store) == ["07:00-22:00"]


def test_deleting_last_window_leaves_day_empty_not_default(store):
    cb(store, "sch:x:2:07002200")
    assert windows(store) == []
    assert not store.snapshot().schedule.is_open(__import__("datetime").datetime(2026, 11, 4, 12, 0, tzinfo=store.snapshot().schedule.tz))
    assert windows(store, 3) == ["07:00-22:00"]  # other days untouched


def test_on_off_buttons_set_state_instead_of_flipping(store):
    cb(store, "sch:t:2:0")
    cb(store, "sch:t:2:0")  # pressing an old "switch off" button again
    assert store.snapshot().schedule.days[WED].enabled is False
    cb(store, "sch:t:2:1")
    assert store.snapshot().schedule.days[WED].enabled is True


@pytest.mark.parametrize("data", ["sch:t:7:0", "sch:c:-1", "sch:x:9:07002200", "sch:x:2:7-22", "sch:d:x"])
def test_bad_callbacks_fall_back_to_menu(store, data):
    text, _ = cb(store, data)
    assert text.startswith("📅 Расписание")


def test_callback_data_fits_telegram_limit(store):
    add(store, ui.DEFAULTS_TARGET, "23:55", "00:05")
    _, markup = ui.day_screen(store, ui.DEFAULTS_TARGET)
    _, tz_markup = ui.tz_screen(1)
    for kb in (markup, tz_markup):
        for row in kb.keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64
    longest = max(ui.ALL_TZ, key=len)
    assert len(f"sch:z:s:{longest}".encode()) <= 64
