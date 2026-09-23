import unittest

from src.rsc import DailySetItem


class TestDailySetItem(unittest.TestCase):
    def make_item(self, destination="", title="Task"):
        return DailySetItem(
            offer_id="offer",
            points=10,
            is_completed=False,
            destination=destination,
            title=title,
            description="",
            date="",
            is_locked=False,
        )

    def test_daily_poll_is_url_offer(self):
        item = self.make_item(
            "https://www.bing.com/search?q=example&filters=PollScenarioId%3A%22POLL_US_RewardsDailyPoll_20260922%22",
            "Daily poll",
        )
        self.assertEqual(item.activity_type, "URL_OFFER")

    def test_supersonic_quiz_is_url_offer(self):
        item = self.make_item(
            "https://www.bing.com/search?q=example&filters=BTEPOKey%3A%22REWARDSQUIZ_DailySet_UrlOffer%22",
            "Supersonic quiz",
        )
        self.assertEqual(item.activity_type, "URL_OFFER")


if __name__ == "__main__":
    unittest.main()
