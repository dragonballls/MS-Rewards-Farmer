import logging
import re
import time

from selenium.common import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from src.browser import Browser
from src.constants import REWARDS_DASHBOARD_URL
from src.rsc import DailySetItem
from src.utils import (
    CONFIG,
    APPRISE,
    cooldown,
    IGNORED_ACTIVITIES,
)


class Activities:
    """
    Class to handle activities in MS Rewards.
    """

    def __init__(self, browser: Browser):
        self.browser = browser
        self.webdriver = browser.webdriver

    def _click_activity_anchor(self, item: DailySetItem, reload_fn=None) -> bool:
        """
        Find and JS-click the dashboard card anchor for item.
        Returns True if found and clicked, False if the anchor is not in the
        current slide's DOM (caller must advance the carousel and retry).

        reload_fn is called to reload the page if the anchor isn't rendered yet;
        it defaults to goToRewards (dashboard) but callers on other pages (e.g.
        the /earn page) pass their own so the retry reloads the right page.
        """
        if reload_fn is None:
            reload_fn = self.browser.utils.goToRewards
        token = item.url_selector_token
        anchors = self.webdriver.find_elements(
            By.XPATH, f"//a[contains(@href, '{token}')]"
        )
        if not anchors:
            sample = [
                a.get_attribute("href")[:80]
                for a in self.webdriver.find_elements(
                    By.XPATH, "//a[contains(@href, 'bing.com/search')]"
                )[:5]
            ]
            logging.debug("[ACTIVITY] No anchor for token %r; bing search hrefs in DOM: %s", token, sample)
            return False

        # Retry once: if the anchor has no size on first load, reload the dashboard
        # and look for it again. The card click is required — direct URL navigation
        # does not award points.
        for attempt in range(1, 3):
            anchor = anchors[0]
            self.webdriver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", anchor
            )
            try:
                WebDriverWait(self.webdriver, 5).until(
                    lambda d: d.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return r.width>0 && r.height>0;",
                        anchor,
                    )
                )
                break  # anchor is rendered, proceed to click
            except TimeoutException:
                if attempt < 2:
                    logging.info(
                        "[ACTIVITY] Anchor not rendered for '%s', reloading page (attempt %d)",
                        cleanupActivityTitle(item.title), attempt,
                    )
                    reload_fn()
                    anchors = self.webdriver.find_elements(
                        By.XPATH, f"//a[contains(@href, '{token}')]"
                    )
                    if not anchors:
                        logging.warning(
                            "[ACTIVITY] Anchor for '%s' disappeared after reload",
                            cleanupActivityTitle(item.title),
                        )
                        return False
                else:
                    logging.warning(
                        "[ACTIVITY] Anchor for '%s' still has no size after reload — skipping",
                        cleanupActivityTitle(item.title),
                    )
                    return False

        original_handle = self.webdriver.current_window_handle
        handles_before = set(self.webdriver.window_handles)

        # React/React-Aria controls require a real pointer click. A JavaScript
        # click can bypass the event handlers that register the Rewards activity.
        for attempt in range(3):
            try:
                anchor = self.webdriver.find_elements(
                    By.XPATH, f"//a[contains(@href, '{token}')]"
                )[0]
                ActionChains(self.webdriver).move_to_element(anchor).click().perform()
                break
            except (
                StaleElementReferenceException,
                ElementClickInterceptedException,
                ElementNotInteractableException,
                IndexError,
            ):
                if attempt == 2:
                    raise
        logging.info(
            "[ACTIVITY] [%s] Clicked '%s'",
            item.activity_type, cleanupActivityTitle(item.title),
        )

        destination_handle = original_handle
        try:
            WebDriverWait(self.webdriver, 8).until(
                lambda d: len(d.window_handles) > len(handles_before)
                or d.current_window_handle != original_handle
                or d.current_url != REWARDS_DASHBOARD_URL
            )
        except TimeoutException:
            pass

        if len(self.webdriver.window_handles) > len(handles_before):
            destination_handle = next(
                h for h in self.webdriver.window_handles if h not in handles_before
            )
            self.webdriver.switch_to.window(destination_handle)

        try:
            WebDriverWait(self.webdriver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass

        # Daily Poll / Supersonic and other quiz-style cards require interaction
        # after the Rewards card opens. Complete the quiz/poll before closing the
        # destination tab; otherwise the dashboard card remains incomplete.
        self._complete_interactive_activity(item)

        if original_handle in self.webdriver.window_handles:
            self.webdriver.switch_to.window(original_handle)
        self.browser.utils.resetTabs()
        cooldown()
        return True

    def _find_visible(self, locators):
        for by, selector in locators:
            try:
                for element in self.webdriver.find_elements(by, selector):
                    try:
                        if element.is_displayed() and element.is_enabled():
                            return element
                    except StaleElementReferenceException:
                        continue
            except (NoSuchElementException, StaleElementReferenceException):
                continue
        return None

    def _click_locator(self, locator, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            try:
                element = self._find_visible([locator])
                if element is not None:
                    ActionChains(self.webdriver).move_to_element(element).click().perform()
                    return True
            except (
                StaleElementReferenceException,
                ElementClickInterceptedException,
                ElementNotInteractableException,
            ):
                pass
        return False

    def _wait_for_activity_change(self, before_source, timeout=4):
        try:
            WebDriverWait(self.webdriver, timeout).until(
                lambda d: d.page_source != before_source
                or self._find_visible([
                    (By.ID, "quizCompleteContainer"),
                    (By.CSS_SELECTOR, "[data-testid='quizCompleteContainer']"),
                ]) is not None
            )
        except TimeoutException:
            pass

    def _complete_interactive_activity(self, item: DailySetItem) -> bool:
        title = cleanupActivityTitle(item.title).lower()
        destination = (item.destination or "").lower()

        is_poll = "poll" in title or "pollscenarioid" in destination
        is_quiz = (
            "quiz" in title
            or "quiz" in destination
            or self._find_visible([(By.ID, "rqStartQuiz"), (By.ID, "rqAnswerOption0")]) is not None
        )

        if not is_poll and not is_quiz:
            return True

        if is_poll:
            poll_locators = [
                (By.ID, "btoption0"),
                (By.ID, "btoption1"),
                (By.ID, "OptionText00"),
                (By.ID, "OptionText01"),
                (By.CSS_SELECTOR, "[id^='btoption']"),
                (By.CSS_SELECTOR, "[id^='OptionText0']"),
            ]
            before = self.webdriver.page_source
            if not self._click_locator_any(poll_locators, timeout=10):
                logging.warning("[ACTIVITY] Could not select a poll option for '%s'", item.title)
                return False
            self._wait_for_activity_change(before, 6)
            logging.info("[ACTIVITY] Completed poll '%s'", cleanupActivityTitle(item.title))
            return True

        start_locators = [(By.ID, "rqStartQuiz")]
        if self._find_visible(start_locators):
            self._click_locator((By.ID, "rqStartQuiz"), timeout=8)
            self._wait_for_activity_change("", 3)

        last_signature = None
        for _ in range(15):
            if self._find_visible([
                (By.ID, "quizCompleteContainer"),
                (By.CSS_SELECTOR, "[data-testid='quizCompleteContainer']"),
            ]):
                logging.info("[ACTIVITY] Completed quiz '%s'", cleanupActivityTitle(item.title))
                return True

            options = []
            for index in range(8):
                try:
                    element = self._find_visible([(By.ID, f"rqAnswerOption{index}")])
                    if element is not None:
                        options.append(element)
                except Exception:
                    continue

            if not options:
                # Some quiz variants expose answer buttons through generic roles.
                for element in self.webdriver.find_elements(
                    By.CSS_SELECTOR, "[role='button'], button"
                ):
                    try:
                        text = (element.text or "").strip()
                        if element.is_displayed() and element.is_enabled() and text:
                            if "next" not in text.lower() and "close" not in text.lower():
                                options.append(element)
                    except StaleElementReferenceException:
                        continue

            if not options:
                page_text = self.webdriver.page_source.lower()
                if "you earned" in page_text or "great job" in page_text:
                    return True
                continue

            signature = tuple(
                (
                    (o.get_attribute("data-option") or "").strip(),
                    (o.text or "").strip(),
                    (o.get_attribute("iscorrectoption") or "").lower(),
                )
                for o in options
            )
            if signature == last_signature and len(options) > 1:
                # Try the next option when the previous click did not advance.
                options = options[1:] + options[:1]
            last_signature = signature

            # Prefer options explicitly marked correct by the Rewards quiz payload.
            chosen = None
            for option in options:
                correct = (
                    (option.get_attribute("iscorrectoption") or "").lower() == "true"
                    or "correctanswer" in (option.get_attribute("class") or "").lower()
                )
                if correct:
                    chosen = option
                    break

            # Some quiz variants expose the correct answer as data in the page.
            if chosen is None:
                match = re.search(
                    r'"correctAnswer"\s*:\s*"([^"]+)"',
                    self.webdriver.page_source,
                    re.IGNORECASE,
                )
                if match:
                    correct_answer = match.group(1)
                    for option in options:
                        if option.get_attribute("data-option") == correct_answer:
                            chosen = option
                            break

            if chosen is None:
                chosen = options[0]

            before = self.webdriver.page_source
            try:
                ActionChains(self.webdriver).move_to_element(chosen).click().perform()
            except StaleElementReferenceException:
                continue
            self._wait_for_activity_change(before, 4)

        logging.warning("[ACTIVITY] Quiz '%s' did not reach a completion state", item.title)
        return False

    def _click_locator_any(self, locators, timeout=8):
        for locator in locators:
            if self._click_locator(locator, timeout=timeout):
                return True
        return False

    def completeActivities(self):
        logging.info("[ACTIVITIES] Trying to complete all activities...")

        # Read dashboard RSC — only today's items have clickable anchors.
        # The RSC payload includes 3 days of items (today + 2 future days).
        dashboard = self.browser.utils.getDashboardData()
        items = dashboard.todays_daily_set()

        todo = []
        for item in items:
            title = cleanupActivityTitle(item.title)
            atype = item.activity_type
            if item.is_completed:
                continue
            if item.is_locked:
                continue
            if item.points == 0:
                continue
            if title in IGNORED_ACTIVITIES:
                continue
            if not item.destination:
                logging.warning("[ACTIVITY] No destination for '%s', skipping", title)
                continue
            todo.append(item)

        if not todo:
            logging.info("[ACTIVITIES] Nothing to do today.")
            logging.info("[ACTIVITIES] Done")
            return

        logging.info("[ACTIVITIES] %d items to complete today", len(todo))

        # Wait up to 10 s for the first expected anchor to appear in the DOM.
        # React hydration can lag the initial HTML on slower machines, so anchors
        # that are present in the RSC payload may not yet be in the live DOM.
        first_token = todo[0].url_selector_token
        try:
            WebDriverWait(self.webdriver, 10).until(
                lambda d: d.find_elements(
                    By.XPATH, f"//a[contains(@href, '{first_token}')]"
                )
            )
        except TimeoutException:
            logging.info(
                "[ACTIVITIES] Anchors not in DOM after 10 s — reloading dashboard once"
            )
            self.browser.utils.goToRewards()

        remaining = list(todo)
        for pass_number in range(1, 3):
            next_remaining = []
            for item in remaining:
                clicked = self._click_activity_anchor(item)
                if not clicked:
                    logging.warning(
                        "[ACTIVITY] No anchor found for '%s' (token=%r) — skipping",
                        cleanupActivityTitle(item.title), item.url_selector_token,
                    )
                    next_remaining.append(item)
                elif not self._wait_until_item_completed(item, timeout=8):
                    logging.warning(
                        "[ACTIVITY] '%s' was opened but is still not marked complete",
                        cleanupActivityTitle(item.title),
                    )
                    next_remaining.append(item)

            if not next_remaining:
                remaining = []
                break

            if pass_number < 2:
                logging.info(
                    "[ACTIVITIES] %d Daily Set item(s) still incomplete; refreshing and retrying",
                    len(next_remaining),
                )
                dashboard = self.browser.utils.getDashboardData()
                status_by_offer = {
                    item.offer_id: item.is_completed
                    for item in dashboard.todays_daily_set()
                }
                remaining = [
                    item
                    for item in next_remaining
                    if not status_by_offer.get(item.offer_id, False)
                ]
            else:
                remaining = next_remaining

        if remaining:
            logging.warning(
                "[ACTIVITIES] %d Daily Set item(s) remain incomplete after retries: %s",
                len(remaining),
                [cleanupActivityTitle(item.title) for item in remaining],
            )

        logging.info("[ACTIVITIES] Done")

        if CONFIG.get("apprise.notify.incomplete-activity"):
            self._notify_incomplete()

    def completeMoreActivities(self):
        """Complete the simple 'Keep earning' cards on rewards.bing.com/earn
        (issue #37) — the navigate-to-earn tasks worth points (e.g. the +10
        featured-topic searches). Promotional banners, the Copilot image task,
        and the multi-search task (points=0) are left for now.
        """
        logging.info("[MORE ACTIVITIES] Trying to complete 'Keep earning' cards...")

        # getEarnData navigates to /earn and parses its RSC, so the anchors are
        # already on the page we're on.
        data = self.browser.utils.getEarnData()
        todo = data.eligible_activity_cards()

        if not todo:
            logging.info("[MORE ACTIVITIES] Nothing to do.")
            return

        logging.info("[MORE ACTIVITIES] %d card(s) to complete", len(todo))

        for item in todo:
            # Each successful click resets tabs back to the /dashboard, so return
            # to /earn and wait for this card's anchor (React hydration can lag)
            # before clicking it.
            self.browser.utils.goToEarn()
            token = item.url_selector_token
            try:
                WebDriverWait(self.webdriver, 10).until(
                    lambda d, t=token: d.find_elements(
                        By.XPATH, f"//a[contains(@href, '{t}')]"
                    )
                )
            except TimeoutException:
                logging.warning(
                    "[MORE ACTIVITY] Anchor for '%s' (token=%r) not in DOM — skipping",
                    cleanupActivityTitle(item.title), token,
                )
                continue

            clicked = self._click_activity_anchor(
                item, reload_fn=self.browser.utils.goToEarn
            )
            if not clicked:
                logging.warning(
                    "[MORE ACTIVITY] No anchor found for '%s' (token=%r) — skipping",
                    cleanupActivityTitle(item.title), item.url_selector_token,
                )

        logging.info("[MORE ACTIVITIES] Done")

    def _wait_until_item_completed(self, item: DailySetItem, timeout=8) -> bool:
        offer_id = item.offer_id

        def completed(_driver):
            try:
                data = self.browser.utils.getDashboardData()
                for candidate in data.todays_daily_set():
                    if (
                        (offer_id and candidate.offer_id == offer_id)
                        or cleanupActivityTitle(candidate.title).lower()
                        == cleanupActivityTitle(item.title).lower()
                    ):
                        return candidate.is_completed
            except Exception:
                return False
            return False

        try:
            return WebDriverWait(self.webdriver, timeout, poll_frequency=1).until(completed)
        except TimeoutException:
            return False

    def _notify_incomplete(self):
        items_after = self.browser.utils.getActivities()
        incomplete = [
            cleanupActivityTitle(i.title)
            for i in items_after
            if not i.is_completed and not i.is_locked
            and cleanupActivityTitle(i.title) not in IGNORED_ACTIVITIES
            and i.activity_type != "REFERRAL"
        ]
        if incomplete:
            logging.info("incompleteActivities: %s", incomplete)
            APPRISE.notify(
                '"' + '", "'.join(incomplete) + '"\n' + REWARDS_DASHBOARD_URL,
                f"We found some incomplete activities for {self.browser.email}",
            )


def cleanupActivityTitle(activityTitle: str) -> str:
    return activityTitle.replace("\u200b", "").replace("\xa0", " ")
