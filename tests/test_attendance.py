import threading

from app.pages.login_page import PASSWORD_INPUT
from app.services.attendance import AttendanceService


class FakeButton:
    def __init__(self, cls="v-button v-widget primary v-button-primary"):
        self.cls = cls

    def get_attribute(self, name):
        return self.cls if name == "class" else None

    def is_displayed(self):
        return True


class FakeDriver:
    def __init__(self, login_page: bool):
        self.login_page = login_page

    def find_elements(self, by, value):
        if (by, value) == PASSWORD_INPUT:
            return [object()] if self.login_page else []
        return [FakeButton(), FakeButton("v-button v-widget primary v-disabled")]


def svc(driver):
    return AttendanceService(None, state_provider=None, create_driver=None, wake=threading.Event(), driver=driver)


def test_login_button_is_never_an_attend_button():
    assert svc(FakeDriver(login_page=True))._clickable_attend_buttons() == []


def test_disabled_buttons_are_skipped():
    assert len(svc(FakeDriver(login_page=False))._clickable_attend_buttons()) == 1


def test_status_snapshot_never_touches_the_driver():
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"driver.{name} touched from /status")
    s = svc(Exploding())
    assert s.snapshot()["driver_open"] is True
