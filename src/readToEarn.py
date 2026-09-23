import logging
import random
import secrets
import time
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from requests_oauthlib import OAuth2Session
from selenium.webdriver.common.by import By

from src.browser import Browser
from .activities import Activities
from .utils import makeRequestsSession, cooldown

# todo Use constant naming style
client_id = "0000000040170455"
authorization_base_url = "https://login.live.com/oauth20_authorize.srf"
token_url = "https://login.live.com/oauth20_token.srf"
redirect_uri = "https://login.live.com/oauth20_desktop.srf"
scope = ["service::prod.rewardsplatform.microsoft.com::MBI_SSL"]


class ReadToEarn:
    """
    Class to handle Read to Earn in MS Rewards.
    """

    def __init__(self, browser: Browser):
        self.browser = browser
        self.webdriver = browser.webdriver
        self.activities = Activities(browser)


    def _find_captured_redirect(self):
        """Return the latest OAuth code redirect captured by Selenium Wire."""
        try:
            requests = list(self.webdriver.requests)
        except Exception:
            return None
        for request in reversed(requests):
            url = getattr(request, "url", "") or ""
            if not url.startswith("https://login.live.com/oauth20_desktop.srf"):
                continue
            query = parse_qs(urlparse(url).query)
            if query.get("code", [None])[0]:
                logging.info("[READ TO EARN] Captured OAuth authorization-code redirect before browser navigation.")
                return url
        return None

    def _find_visible(self, locators):
        for by, selector in locators:
            try:
                for element in self.webdriver.find_elements(by, selector):
                    try:
                        if element.is_displayed():
                            return element
                    except StaleElementReferenceException:
                        continue
            except (NoSuchElementException, StaleElementReferenceException):
                continue
        return None

    def _click_visible(self, locators, timeout=10):
        end = time.time() + timeout
        last_error = None
        while time.time() < end:
            element = self._find_visible(locators)
            if element is None:
                time.sleep(0.25)
                continue
            try:
                element.click()
                return True
            except (
                StaleElementReferenceException,
                ElementClickInterceptedException,
                ElementNotInteractableException,
            ) as exc:
                last_error = exc
                time.sleep(0.25)
        if last_error:
            logging.debug("[READ TO EARN] Click retry exhausted: %s", last_error)
        return False

    def _is_fido_login_page(self):
        return (
            "/fido/" in self.webdriver.current_url.lower()
            or "sign in to your account" in (self.webdriver.title or "").lower()
        )

    def _is_alternate_signin_page(self):
        title = (self.webdriver.title or "").lower()
        return "sign in another way" in title

    def _password_flow(self, timeout=15):
        password_locators = [
            (By.NAME, "passwd"),
            (By.ID, "passwordEntry"),
            (By.ID, "i01115"),
        ]
        use_password_locators = [
            (By.CSS_SELECTOR, '[aria-label="Use your password"]'),
            (
                By.XPATH,
                "//*[self::button or self::a or @role='button' or @role='link']"
                "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'use your password')]",
            ),
            (
                By.XPATH,
                "//*[self::button or self::a or @role='button' or @role='link']"
                "[normalize-space()='Password']",
            ),
        ]

        for _ in range(3):
            field = self._find_visible(password_locators)
            if field is not None:
                try:
                    field.click()
                    field.clear()
                    field.send_keys(self.browser.password)
                    if field.get_attribute("value") == self.browser.password:
                        break
                except StaleElementReferenceException:
                    continue
        else:
            choice = self._find_visible(use_password_locators)
            if choice is None:
                return False
            if not self._click_visible(use_password_locators, timeout):
                return False
            try:
                field = WebDriverWait(self.webdriver, timeout).until(
                    EC.any_of(
                        EC.element_to_be_clickable((By.NAME, "passwd")),
                        EC.element_to_be_clickable((By.ID, "passwordEntry")),
                        EC.element_to_be_clickable((By.ID, "i01115")),
                    )
                )
                field.click()
                field.clear()
                field.send_keys(self.browser.password)
            except (TimeoutException, StaleElementReferenceException):
                return False

        if not self._click_visible([
            (By.CSS_SELECTOR, "[data-testid='primaryButton']"),
            (By.ID, "idSIButton9"),
            (By.ID, "primaryButton"),
        ], timeout):
            return False

        # Optional "Stay signed in?" / KMSI page.
        self._click_visible([
            (By.XPATH, "//button[normalize-space()='Yes']"),
            (By.ID, "acceptButton"),
            (By.ID, "idSIButton9"),
        ], timeout=5)

        try:
            WebDriverWait(self.webdriver, timeout).until(
                lambda d: d.current_url.startswith(
                    "https://login.live.com/oauth20_desktop.srf?code="
                )
            )
            return True
        except TimeoutException:
            return False

    def _complete_fido_password_flow(self):
        # Prefer a password field/option already exposed by Microsoft's current
        # FIDO page. Some variants render the password choice without an
        # "Other ways" button.
        if self._password_flow(timeout=5):
            return True

        alternate = [
            (
                By.XPATH,
                "//*[self::button or self::a or @role='button' or @role='link']"
                "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in another way')]",
            ),
            (
                By.XPATH,
                "//*[self::button or self::a or @role='button' or @role='link']"
                "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'other ways to sign in')]",
            ),
            (By.CSS_SELECTOR, "[data-testid='secondaryButton']"),
        ]
        if not self._click_visible(alternate, timeout=8):
            return False
        return self._password_flow(timeout=15)

    def _complete_alternate_password_flow(self):
        return self._password_flow(timeout=5) or self._complete_fido_password_flow()

    def completeReadToEarn(self):

        logging.info("[READ TO EARN] " + "Trying to complete Read to Earn...")

        accountName = self.browser.email
        mobileApp = makeRequestsSession(
            OAuth2Session(client_id, scope=scope, redirect_uri=redirect_uri)
        )
        # Reuse the authenticated Rewards browser session. Do not force prompt=none:
        # Microsoft's consumer login can legitimately require an interactive
        # password/passkey step before issuing the authorization code.
        authorization_url = mobileApp.authorization_url(
            authorization_base_url,
            access_type="offline_access",
            login_hint=accountName,
        )[0]

        # Clear captured requests so a stale OAuth code cannot belong to another run.
        try:
            del self.webdriver.requests
        except Exception:
            pass

        # Microsoft can issue the short-lived code redirect and immediately navigate
        # to ?removed=true. Capture the redirect at the network layer before it vanishes.
        self.webdriver.get(authorization_url)
        count = 0
        oauth_login_recovered = False
        redirect_response = None
        while count < 20:
            current_url = self.webdriver.current_url

            if current_url.startswith("https://login.live.com/oauth20_desktop.srf?code="):
                redirect_response = current_url
                break

            redirect_response = self._find_captured_redirect()
            if redirect_response:
                break

            if not oauth_login_recovered and self._is_fido_login_page():
                logging.info("[READ TO EARN] OAuth reached Microsoft's FIDO/passkey page; switching to password sign-in.")
                if self._complete_fido_password_flow():
                    oauth_login_recovered = True
                    count = 0
                    continue

            if not oauth_login_recovered and self._is_alternate_signin_page():
                logging.info("[READ TO EARN] OAuth reached Microsoft's alternate sign-in chooser; selecting password.")
                if self._complete_alternate_password_flow():
                    oauth_login_recovered = True
                    count = 0
                    continue

            logging.info("[READ TO EARN] Waiting for OAuth redirect (URL: %s)", current_url)
            time.sleep(0.5)
            count += 1

        if not redirect_response:
            visible_buttons = []
            for b in self.webdriver.find_elements(By.XPATH, "//button | //*[@role='button']"):
                try:
                    if b.is_displayed():
                        visible_buttons.append(
                            (b.text or "").strip()
                            or b.get_attribute("id")
                            or b.get_attribute("data-testid")
                        )
                except Exception:
                    continue
            logging.error(
                "[READ TO EARN] Stuck waiting for OAuth redirect. "
                "URL: %s | Title: %s | visible buttons: %s",
                self.webdriver.current_url, self.webdriver.title, visible_buttons,
            )
            raise Exception("Stuck in waiting for login")

        logging.info("[READ TO EARN] Logged-in successfully !")
        token = mobileApp.fetch_token(
            token_url, authorization_response=redirect_response, include_client_id=True
        )
        # Do Daily Check in
        json_data = {
            "amount": 1,
            "country": self.browser.localeGeo.lower(),
            "id": secrets.token_hex(64),
            "type": 101,
            "attributes": {
                "offerid": "Gamification_Sapphire_DailyCheckIn",
            },
        }
        logging.info("[READ TO EARN] Daily App Check In")
        r = mobileApp.post(
            "https://prod.rewardsplatform.microsoft.com/dapi/me/activities",
            json=json_data,
        )
        balance = r.json().get("response").get("balance")
        time.sleep(random.randint(10, 20))

        # json data to confirm an article is read
        json_data = {
            "amount": 1,
            "country": self.browser.localeGeo.lower(),
            "id": 1,
            "type": 101,
            "attributes": {
                "offerid": "ENUS_readarticle3_30points",
            },
        }

        # 10 is the most articles you can read. Sleep time is a guess, not tuned
        for i in range(10):
            # Replace ID with a random value so get credit for a new article
            json_data["id"] = secrets.token_hex(64)
            r = mobileApp.post(
                "https://prod.rewardsplatform.microsoft.com/dapi/me/activities",
                json=json_data,
            )
            newbalance = r.json().get("response").get("balance")

            if newbalance == balance:
                logging.info("[READ TO EARN] Read All Available Articles !")
                break

            logging.info("[READ TO EARN] Read Article " + str(i + 1))
            balance = newbalance
            cooldown()

        logging.info("[READ TO EARN] Completed the Read to Earn successfully !")
