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

        # Microsoft hydrates the Bing-hosted quiz/poll module after the SERP is ready.
        # Wait for that module explicitly instead of starting the solver against an
        # otherwise-complete document, which is the failure seen on current Bing.
        try:
            WebDriverWait(self.webdriver, 20).until(
                lambda d: bool(d.find_elements(By.CSS_SELECTOR, ".btp_card, .btom_card, .btq_main"))
                or "bing.com/search" not in d.current_url.lower()
            )
        except TimeoutException:
            logging.debug(
                "[ACTIVITY] No current Bing interactive module appeared for '%s' within 20s",
                cleanupActivityTitle(item.title),
            )

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
        """Complete the quiz/poll on the Bing destination page."""
        title = cleanupActivityTitle(item.title)
        destination = (item.destination or "").lower()

        poll_selectors = [
            (By.CSS_SELECTOR, ".btp_choices .btp_choice a[href]"),
            (By.CSS_SELECTOR, ".btp_choices .btp_choice"),
        ]
        quiz_selectors = [
            (By.CSS_SELECTOR, ".btom_opts .btom_opt a[href]"),
            (By.CSS_SELECTOR, ".btq_opts .btq_opt a[href]"),
            (By.CSS_SELECTOR, ".btom_opts .btom_opt"),
            (By.CSS_SELECTOR, ".btq_opts .btq_opt"),
        ]
        next_selectors = [
            (By.CSS_SELECTOR, ".btq_nxtQues button"),
            (By.CSS_SELECTOR, ".btq_nxtQues a[href]"),
            (By.CSS_SELECTOR, "button[title='Next']"),
            (By.CSS_SELECTOR, "[aria-label='Next']"),
        ]

        def visible(locators):
            for locator in locators:
                try:
                    for element in self.webdriver.find_elements(*locator):
                        try:
                            # Match the maintained September 2026 Bing module pattern:
                            # the clickable child is a real anchor and must be on-screen.
                            rect = element.rect
                            if (
                                element.is_displayed()
                                and element.is_enabled()
                                and rect.get("width", 0) > 0
                                and rect.get("height", 0) > 0
                            ):
                                return element
                        except StaleElementReferenceException:
                            continue
                except (NoSuchElementException, StaleElementReferenceException):
                    continue
            return None

        def changed(before_url, before_source, timeout=8):
            try:
                WebDriverWait(self.webdriver, timeout).until(
                    lambda d: d.current_url != before_url or d.page_source != before_source
                )
            except TimeoutException:
                pass

        # Current Bing poll: one visible choice is the whole activity.
        # Wait specifically for a live poll choice. The poll module can be
        # present before its choices hydrate.
        try:
            WebDriverWait(self.webdriver, 15).until(lambda d: visible(poll_selectors) is not None)
        except TimeoutException:
            pass
        poll = visible(poll_selectors)
        if poll:
            before_url, before_source = self.webdriver.current_url, self.webdriver.page_source
            try:
                self.webdriver.execute_script(
                    "arguments[0].scrollIntoView({block:'center',inline:'center'});", poll
                )
                ActionChains(self.webdriver).move_to_element(poll).click().perform()
                changed(before_url, before_source, timeout=12)
                try:
                    WebDriverWait(self.webdriver, 10).until(
                        lambda d: bool(d.find_elements(By.CSS_SELECTOR, ".btp_voted, .btp_percentage, .btp_selected"))
                        or d.current_url != before_url
                    )
                except TimeoutException:
                    pass
                if self.webdriver.current_url != before_url or self.webdriver.find_elements(
                    By.CSS_SELECTOR, ".btp_voted, .btp_percentage, .btp_selected"
                ):
                    logging.info("[ACTIVITY] Completed poll '%s'", title)
                    return True
            except (
                ElementClickInterceptedException,
                ElementNotInteractableException,
                StaleElementReferenceException,
            ):
                pass

        # Current Bing quiz: each answer navigates/re-renders the SERP. Re-locate
        # after every click and use the quiz's explicit Next control when a reveal
        # screen is shown between questions.
        for _ in range(10):
            option = visible(quiz_selectors)
            if option:
                before_url, before_source = self.webdriver.current_url, self.webdriver.page_source
                try:
                    ActionChains(self.webdriver).move_to_element(option).click().perform()
                    changed(before_url, before_source)
                    continue
                except (
                    ElementClickInterceptedException,
                    ElementNotInteractableException,
                    StaleElementReferenceException,
                ):
                    continue

            next_button = visible(next_selectors)
            if next_button:
                before_url, before_source = self.webdriver.current_url, self.webdriver.page_source
                try:
                    ActionChains(self.webdriver).move_to_element(next_button).click().perform()
                    changed(before_url, before_source)
                    continue
                except (
                    ElementClickInterceptedException,
                    ElementNotInteractableException,
                    StaleElementReferenceException,
                ):
                    continue

            page_text = (self.webdriver.page_source or "").lower()
            if any(marker in page_text for marker in (
                "quizcompletecontainer",
                "quiz complete",
                "you got ",
                "great job",
            )):
                logging.info("[ACTIVITY] Completed quiz '%s'", title)
                return True
            break

        # Legacy Rewards quiz compatibility.
        is_quiz = (
            "quiz" in title.lower()
            or "quiz" in destination
            or self._find_visible([
                (By.ID, "rqStartQuiz"),
                (By.ID, "rqAnswerOption0"),
            ]) is not None
        )
        if is_quiz:
            self._click_locator((By.ID, "rqStartQuiz"), timeout=5)
            for _ in range(12):
                option = None
                for index in range(8):
                    option = self._find_visible([(By.ID, f"rqAnswerOption{index}")])
                    if option:
                        break
                if option is None:
                    if self._find_visible([
                        (By.ID, "quizCompleteContainer"),
                        (By.CSS_SELECTOR, "[data-testid='quizCompleteContainer']"),
                    ]):
                        logging.info("[ACTIVITY] Completed quiz '%s'", title)
                        return True
                    continue
                try:
                    ActionChains(self.webdriver).move_to_element(option).click().perform()
                    time.sleep(0.5)
                except (
                    ElementClickInterceptedException,
                    ElementNotInteractableException,
                    StaleElementReferenceException,
                ):
                    continue

            if self._find_visible([
                (By.ID, "quizCompleteContainer"),
                (By.CSS_SELECTOR, "[data-testid='quizCompleteContainer']"),
            ]):
                logging.info("[ACTIVITY] Completed quiz '%s'", title)
                return True

        # URL offers are complete by opening them. Only known interactive
        # activities should report failure.
        if (
            "poll" not in title.lower()
            and "quiz" not in title.lower()
            and "dsetqu" not in destination
        ):
            return True

        logging.warning(
            "[ACTIVITY] Interactive activity '%s' did not reach a completion state",
            title,
        )
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
