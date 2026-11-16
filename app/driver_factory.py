from selenium import webdriver
from selenium.webdriver.remote.webdriver import WebDriver

def make_driver(remote_url: str) -> WebDriver:
    options = webdriver.ChromeOptions()
    # TLS is verified: the portal has a valid public certificate, and the WSP
    # password is typed into whatever answers at the portal's address.
    options.add_argument("--headless=new")
    # Fixed viewport so /screen screenshots show the whole attendance page.
    options.add_argument("--window-size=1366,900")
    # Keep the grid container small: avoid /dev/shm usage (Chrome falls back
    # to /tmp), so the 2gb default shm_size is unnecessary.
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Remote(command_executor=remote_url, options=options)
