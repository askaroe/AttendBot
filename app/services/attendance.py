import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
    InvalidSessionIdException,
)
from selenium.webdriver.remote.webdriver import WebDriver

from ..telegram import TelegramNotifier
from ..pages.login_page import LoginPage, on_login_page, wait_vaadin_ready
from ..store import RuntimeState

logger = logging.getLogger(__name__)

CONFIRM_SECS = 10                 # how long a click gets to visibly take effect
UNCONFIRMED_NOTIFY_EVERY = 600    # at most one "click had no effect" alert per lesson per 10 min
LOGIN_RETRY_LIMIT = 3             # consecutive failed logins before backing off …
LOGIN_BACKOFF_SECS = 30 * 60      # … for this long, so the portal account is not locked


class AttendanceService:
    """Schedule-aware attendance loop.

    Re-reads RuntimeState (credentials, base URL, schedule, paused flag) every
    iteration, so Telegram commands take effect on the next tick — or
    immediately, because every sleep is interruptible via the shared wake
    event. The WebDriver session is owned exclusively by this (main) thread;
    /screen reaches it only through the ``screenshot`` event.
    """

    ATTEND_BTN = (By.XPATH, "//div[contains(@class,'v-button') and contains(@class,'primary')]")

    def __init__(
        self,
        telegram: TelegramNotifier,
        *,
        state_provider: Callable[[], RuntimeState],
        create_driver: Callable[[], WebDriver],
        wake: threading.Event,
        screenshot: Optional[threading.Event] = None,
        wait_seconds: int = 20,
        driver: Optional[WebDriver] = None,
    ) -> None:
        self.driver: Optional[WebDriver] = driver
        self._wait_seconds = wait_seconds
        self.wait: Optional[WebDriverWait] = WebDriverWait(driver, wait_seconds) if driver else None
        self.login_page: Optional[LoginPage] = LoginPage(driver) if driver else None

        self.tg = telegram
        self._state_provider = state_provider
        self._create_driver = create_driver
        self._wake = wake
        self._screenshot = screenshot
        self._driver_url: Optional[str] = None
        self._current_url: Optional[str] = None  # cached for /status (other thread)
        self._last_lesson: Optional[str] = None
        self._last_attend_at: Optional[str] = None
        self._unconfirmed_alerts: Dict[str, float] = {}
        self._login_failures = 0
        self._login_blocked: Optional[Tuple[str, str]] = None
        self._login_blocked_until = 0.0
        self._last_good_login: Optional[Tuple[str, str]] = None

        logger.info("AttendanceService initialized (wait_seconds=%s)", wait_seconds)

    # ---------- infra ----------

    def _notify(self, text: str) -> None:
        try:
            self.tg.send_message(text)
        except Exception:
            logger.warning("Failed to send Telegram notification")

    def _send_screenshot(self, caption: str) -> None:
        """Best-effort photo of the current page; falls back to text."""
        png = None
        if self.driver:
            try:
                png = self.driver.get_screenshot_as_png()
            except WebDriverException:
                logger.warning("Screenshot failed", exc_info=True)
        if png is None:
            self._notify(caption)
            return
        try:
            self.tg.send_photo(png, caption)
        except Exception:
            logger.warning("Failed to send Telegram photo", exc_info=True)

    def _serve_screenshot_request(self) -> None:
        if not (self._screenshot and self._screenshot.is_set()):
            return
        self._screenshot.clear()
        if not self.driver:
            self._notify("📸 Браузер сейчас закрыт — вне окна расписания, на паузе или не задан /login.")
            return
        self._send_screenshot(f"📸 {self._safe_url()}")

    def _sleep(self, secs: float) -> None:
        """Interruptible sleep: returns early when a command mutated state."""
        if self._wake.wait(max(0.0, secs)):
            self._wake.clear()

    def _rebind_driver(self, driver: WebDriver) -> None:
        self.driver = driver
        self.wait = WebDriverWait(driver, self._wait_seconds)
        self.login_page = LoginPage(driver)

    def _open_driver(self, reason: str, base_url: str) -> None:
        logger.info("Opening WebDriver (reason: %s)", reason)
        drv = self._create_driver()
        self._rebind_driver(drv)
        drv.get(base_url)
        try:
            drv.add_cookie({"name": "r5-locale", "value": "ru"})
            drv.refresh()  # server resolves the locale per request
        except WebDriverException:
            logger.warning("Failed to set r5-locale=ru cookie", exc_info=True)
        self._driver_url = base_url

    def _try_open(self, reason: str, base_url: str) -> bool:
        """Open the driver without letting a grid outage crash the loop."""
        try:
            self._open_driver(reason, base_url)
            return True
        except Exception:
            logger.exception("Failed to open WebDriver (%s); retrying next tick", reason)
            self._shutdown_driver("failed to open")
            return False

    def _shutdown_driver(self, reason: str) -> None:
        if not self.driver:
            return
        logger.info("Closing WebDriver (reason: %s)", reason)
        try:
            self.driver.quit()
        except Exception:
            logger.exception("Error while quitting WebDriver")
        finally:
            self.driver = None
            self.wait = None
            self.login_page = None
            self._driver_url = None
            self._current_url = None

    def _safe_url(self) -> str:
        try:
            return self.driver.current_url if self.driver else "<no-driver>"
        except Exception:
            return "<unavailable>"

    # ---------- domain ----------

    def ensure_logged_in(self, username: str, password: str) -> bool:
        """Log in if the login form is showing. False = not logged in (yet).

        Repeated failures back off for LOGIN_BACKOFF_SECS (or until /login
        changes the credentials) instead of retrying every few seconds, which
        could lock the portal account.
        """
        if not (self.driver and self.wait and self.login_page):
            raise RuntimeError("Driver is not initialized")
        logger.debug("Checking login state at %s", self._safe_url())
        if not self.login_page.at_login():
            return True

        creds = (username, password)
        if creds == self._login_blocked and time.monotonic() < self._login_blocked_until:
            return False

        logger.info("Detected login screen → attempting login")
        try:
            self.login_page.login(username, password)
            ok = self.login_page.wait_logged_in()
        except TimeoutException:
            ok = False

        if ok:
            logger.info("Login succeeded")
            if creds != self._last_good_login:
                self._notify(f"🔓 Вход в портал выполнен ({username}). Слежу за отметками.")
            self._last_good_login = creds
            self._login_failures = 0
            self._login_blocked = None
            return True

        self._login_failures += 1
        logger.warning("Login attempt failed (%d in a row)", self._login_failures)
        if self._login_failures >= LOGIN_RETRY_LIMIT:
            self._login_failures = 0
            self._login_blocked = creds
            self._login_blocked_until = time.monotonic() + LOGIN_BACKOFF_SECS
            self._send_screenshot(
                f"❌ Не удалось войти в портал {LOGIN_RETRY_LIMIT} раза подряд. "
                f"Следующая попытка через {LOGIN_BACKOFF_SECS // 60} мин или сразу после "
                "/login <логин> <пароль>."
            )
        return False

    def _clickable_attend_buttons(self) -> List:
        """All attend buttons that are actually clickable right now.

        The teacher can open several attendance cards at once; already-marked
        cards show a non-clickable button («Отмечен»), which must not block
        the rest. Vaadin buttons are divs — the native `disabled` attribute
        does not apply, so disabled state is detected via the v-disabled
        class / aria-disabled attribute. On the login form the primary button
        is «Вход», so nothing there counts as an attend button.
        """
        if not self.driver:
            return []
        out: List = []
        try:
            if on_login_page(self.driver):
                return []
            for b in self.driver.find_elements(*self.ATTEND_BTN):
                try:
                    cls = b.get_attribute("class") or ""
                    if "v-disabled" in cls or b.get_attribute("aria-disabled") == "true":
                        continue
                    if not b.is_displayed():
                        continue
                except StaleElementReferenceException:
                    continue
                out.append(b)
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return []
        return out

    def _poll_buttons(self, _driver) -> List:
        """WebDriverWait predicate; also serves /screen while waiting."""
        self._serve_screenshot_request()
        return self._clickable_attend_buttons() or False

    _TIME_LIKE = re.compile(r"\d{1,2}:\d{2}")

    def _lesson_title_for(self, btn) -> str:
        """Best-effort lesson title from the same card as the button.

        Card labels render in order (title, teacher, time, remaining), so the
        title is the closest preceding v-label-bold that is neither a time
        range nor a "remaining" counter.
        """
        try:
            labels = btn.find_elements(By.XPATH, "preceding::div[contains(@class,'v-label-bold')]")
            texts = [(l.text or "").split("\n")[0].strip() for l in labels[-6:]]
            for t in reversed(texts):
                if not t:
                    continue
                if self._TIME_LIKE.search(t):
                    continue
                if t.lower().startswith(("оста", "қалд", "remain", "калды")):
                    continue
                return t
        except (StaleElementReferenceException, WebDriverException):
            pass
        return ""

    def _click_confirmed(self, before: int) -> bool:
        """True once fewer attend buttons are clickable than before the click,
        and that still holds after the page has settled."""
        try:
            WebDriverWait(self.driver, CONFIRM_SECS, poll_frequency=0.5).until(
                lambda _d: len(self._clickable_attend_buttons()) < before
            )
        except TimeoutException:
            return False
        wait_vaadin_ready(self.driver, 5)
        return len(self._clickable_attend_buttons()) < before

    def _report_unconfirmed(self, title: str) -> None:
        now = time.monotonic()
        if now - self._unconfirmed_alerts.get(title, float("-inf")) < UNCONFIRMED_NOTIFY_EVERY:
            return
        self._unconfirmed_alerts[title] = now
        self._send_screenshot(
            f"⚠️ Нажал «отметиться» ({title}), но кнопка не изменилась. "
            "Продолжаю пробовать — если не выйдет, отметьтесь вручную."
        )

    def try_attend_once(self) -> int:
        """Click every clickable attend button; returns how many were confirmed.

        Raises TimeoutException when no clickable button appears within the
        wait window — i.e. everything is marked / no disciplines are open —
        which keeps the caller's refresh-and-retry cycle going.
        """
        if not (self.driver and self.wait):
            raise RuntimeError("Driver is not initialized")

        logger.debug("Waiting for ATTEND buttons…")
        first = self.wait.until(self._poll_buttons)

        confirmed = 0
        failed: set = set()
        # Re-scan after every click: Vaadin re-renders cards, so handles go stale.
        for _ in range(len(first) + 2):
            buttons = self._clickable_attend_buttons()
            todo = [(b, self._lesson_title_for(b) or "без названия") for b in buttons]
            todo = [(b, t) for b, t in todo if t not in failed]
            if not todo:
                break
            btn, title = todo[0]
            logger.info("Clicking ATTEND (%s) at %s", title, self._safe_url())
            try:
                btn.click()
            except StaleElementReferenceException:
                logger.debug("Button went stale before the click; re-scanning")
                continue
            if self._click_confirmed(len(buttons)):
                confirmed += 1
                self._last_lesson = title
                self._last_attend_at = datetime.now(self._state_provider().schedule.tz).strftime("%Y-%m-%d %H:%M")
                self._unconfirmed_alerts.pop(title, None)
                logger.info("Attendance confirmed: %s", title)
                self._notify(f"✅ Отмечено: {title}")
            else:
                logger.warning("Click on ATTEND (%s) had no visible effect", title)
                failed.add(title)
                self._report_unconfirmed(title)
        return confirmed

    def snapshot(self) -> Dict:
        """Live view for /status (read by the command thread; never touches the driver)."""
        return {
            "driver_open": self.driver is not None,
            "current_url": self._current_url or "",
            "last_lesson": self._last_lesson,
            "last_attend_at": self._last_attend_at,
        }

    # ---------- main loop ----------

    def run_loop(self, poll_secs: int = 5) -> None:
        logger.info("Starting schedule-aware loop (poll=%ss)", poll_secs)

        while True:
            self._serve_screenshot_request()
            st = self._state_provider()
            now = datetime.now(st.schedule.tz)

            if st.paused:
                if self.driver:
                    self._shutdown_driver("paused (/stop)")
                logger.info("Paused — waiting for /start")
                self._sleep(30)
                continue

            if not st.wsp_login or not st.wsp_password:
                if self.driver:
                    self._shutdown_driver("credentials missing")
                logger.warning("WSP credentials not set — waiting for /login")
                self._sleep(30)
                continue

            if not st.base_url:
                if self.driver:
                    self._shutdown_driver("base_url missing")
                logger.warning("BASE_URL not set — set it via env BASE_URL or /url")
                self._sleep(30)
                continue

            secs = st.schedule.seconds_until_next_open(now)
            if secs > 0:
                if self.driver:
                    self._shutdown_driver("outside schedule window")
                wake_at = now + timedelta(seconds=secs)
                logger.info(
                    "Outside schedule — sleeping until %s (%ds)",
                    wake_at.strftime("%Y-%m-%d %H:%M:%S %Z"), secs,
                )
                self._sleep(secs)
                continue

            if not self.driver:
                if not self._try_open("enter schedule window", st.base_url):
                    self._sleep(poll_secs)
                    continue
            elif self._driver_url != st.base_url:
                self._shutdown_driver("base_url changed")
                if not self._try_open("new base_url", st.base_url):
                    self._sleep(poll_secs)
                    continue

            try:
                if self.ensure_logged_in(st.wsp_login, st.wsp_password):
                    self._current_url = self._safe_url()
                    self.try_attend_once()
                    logger.debug("Sleeping %ss (inside window)", poll_secs)
                    self._sleep(poll_secs)
                else:
                    self._sleep(max(poll_secs, 30))

            except InvalidSessionIdException as e:
                logger.exception("InvalidSessionIdException: %s; recreate driver", e)
                self._shutdown_driver("invalid session id")
                time.sleep(2)
                self._try_open("recover invalid session", st.base_url)
                self._sleep(poll_secs)

            except TimeoutException:
                # Normal idle state: nothing to mark right now. Reload to pick up new cards.
                logger.debug("No open attendance at %s; refreshing; next check in %ss", self._safe_url(), poll_secs)
                try:
                    if self.driver:
                        self.driver.refresh()
                except Exception:
                    logger.exception("Failed to refresh after TimeoutException; recreating driver")
                    self._shutdown_driver("refresh failed after timeout")
                    self._try_open("recover after timeout", st.base_url)
                self._sleep(poll_secs)

            except WebDriverException as e:
                logger.exception("WebDriverException: %s; will try refresh", e)
                time.sleep(3)
                try:
                    if self.driver:
                        self.driver.refresh()
                except Exception:
                    logger.exception("Failed to refresh after WebDriverException; recreating driver")
                    self._shutdown_driver("refresh failed after wde")
                    self._try_open("recover after wde", st.base_url)
                self._sleep(poll_secs)

            except Exception:
                logger.exception("Unexpected error in run_loop; retrying in %ss", poll_secs)
                self._sleep(poll_secs)

    def shutdown(self) -> None:
        self._shutdown_driver("service shutdown")
