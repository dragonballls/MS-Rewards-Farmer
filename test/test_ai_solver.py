import unittest

from src.ai_solver import AIAssistant


class TestAIAssistant(unittest.TestCase):
    def test_integer_parser(self):
        self.assertEqual(AIAssistant._integer("2"), 2)
        self.assertEqual(AIAssistant._integer("Option 17"), 17)
        self.assertIsNone(AIAssistant._integer(""))
        self.assertIsNone(AIAssistant._integer(None))

    def test_disabled_without_key(self):
        assistant = AIAssistant()
        self.assertFalse(assistant.available)

    def test_choose_quiz_option_without_key(self):
        assistant = AIAssistant()
        self.assertIsNone(
            assistant.choose_quiz_option(
                "Which option is correct?",
                ["A", "B", "C"],
            )
        )


if __name__ == "__main__":
    unittest.main()
