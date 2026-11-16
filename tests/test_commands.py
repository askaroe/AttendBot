import threading

import pytest

from app.commands import CommandProcessor
from app.store import BotStore


@pytest.fixture
def env(tmp_path):
    store = BotStore(str(tmp_path / "state.json"))
    wake, shot = threading.Event(), threading.Event()
    return store, wake, shot, CommandProcessor(store, wake, lambda: {}, shot)


@pytest.mark.parametrize("url", [
    "https://pge.kbtu.kz/RegistrationOnline",
    "https://wsp.kbtu.kz/RegistrationOnline",
    "https://kbtu.kz/x",
])
def test_url_accepts_kbtu_https(env, url):
    store, wake, _, p = env
    assert p.execute(f"/url {url}").startswith("✅")
    assert store.snapshot().base_url == url and wake.is_set()


@pytest.mark.parametrize("url", [
    "http://pge.kbtu.kz/RegistrationOnline",   # no TLS
    "https://kbtu.kz.evil.example/login",      # look-alike host
    "https://evilkbtu.kz/",                    # suffix without the dot
    "https://user@evil.example/?kbtu.kz",
    "pge.kbtu.kz",
])
def test_url_rejects_everything_else(env, url):
    store, _, _, p = env
    assert not p.execute(f"/url {url}").startswith("✅")
    assert store.snapshot().base_url is None


def test_login_keeps_spaces_and_never_echoes_password(env):
    store, _, _, p = env
    reply = p.execute("/login a_student pass with spaces")
    assert "pass with spaces" not in reply
    st = store.snapshot()
    assert (st.wsp_login, st.wsp_password) == ("a_student", "pass with spaces")


def test_screen_asks_the_attendance_thread(env):
    _, wake, shot, p = env
    p.execute("/screen")
    assert shot.is_set() and wake.is_set()


def test_status_works_without_service(env):
    *_, p = env
    assert "Статус" in p.execute("/status")
