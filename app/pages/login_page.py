import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import TimeoutException

# The login form is the only portal page with a password field (verified on
# pge.kbtu.kz, 2026-11-10). Its «Вход» button is a primary v-button, exactly
# like the attend buttons, so callers must check this before clicking anything.
PASSWORD_INPUT = (By.CSS_SELECTOR, "input[type='password']")

# True once the Vaadin UI has rendered and no client is busy with a request.
_VAADIN_READY_JS = """
const cs = window.vaadin && window.vaadin.clients ? Object.values(window.vaadin.clients) : [];
return document.querySelector('.v-app .v-widget') !== null
    && cs.length > 0 && !cs.some(c => c.isActive && c.isActive());
"""


def wait_vaadin_ready(driver: WebDriver, timeout: float) -> bool:
    """Wait until the page has rendered and settled; False on timeout."""
    try:
        WebDriverWait(driver, timeout, poll_frequency=0.25).until(
            lambda d: d.execute_script(_VAADIN_READY_JS)
        )
        return True
    except TimeoutException:
        return False


def on_login_page(driver: WebDriver) -> bool:
    return bool(driver.find_elements(*PASSWORD_INPUT))


class LoginPage:
    USERNAME = (By.ID, "gwt-uid-4")
    PASSWORD = (By.ID, "gwt-uid-6")
    LOGIN_TITLE = (By.XPATH, "//div[@class='v-button v-widget primary v-button-primary']")

    def __init__(self, driver: WebDriver, wait_seconds: int = 15) -> None:
        self.driver = driver
        self.wait_seconds = wait_seconds
        self.wait = WebDriverWait(driver, wait_seconds)

    def at_login(self) -> bool:
        """Whether the login form is showing, checked once the UI has settled.

        Detects the form by its password field rather than the button caption,
        which differs per locale («Вход», «Кіру», «Log in»).
        """
        wait_vaadin_ready(self.driver, self.wait_seconds)
        return on_login_page(self.driver)

    def wait_logged_in(self, timeout: float = 15) -> bool:
        """After submitting: True once the login form has gone away."""
        try:
            WebDriverWait(self.driver, timeout, poll_frequency=0.5).until(
                lambda d: not on_login_page(d)
            )
        except TimeoutException:
            return False
        wait_vaadin_ready(self.driver, timeout)
        return True

    def login(self, username: str, password: str) -> None:
        """pge.kbtu.kz specifics (verified against the live page):
        - the username field is a Vaadin combobox: type, then accept the
          suggestion (ArrowDown + Enter) — plain send_keys is cleared on blur;
        - Enter in the password field does NOT submit — click the primary
          button («Вход» / «Кіру»).
        """
        user = self.wait.until(EC.element_to_be_clickable(self.USERNAME))
        user.click()
        user.clear()
        user.send_keys(username)
        time.sleep(1.5)  # let Vaadin filter suggestions
        user.send_keys(Keys.ARROW_DOWN, Keys.ENTER)
        self.wait.until(lambda d: (user.get_attribute("value") or "").strip() == username.strip())

        pwd = self.wait.until(EC.element_to_be_clickable(self.PASSWORD))
        pwd.clear()
        pwd.send_keys(password)

        submit = self.wait.until(EC.element_to_be_clickable(self.LOGIN_TITLE))
        submit.click()
