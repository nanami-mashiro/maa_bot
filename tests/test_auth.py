import unittest

from maa_tg_bot.auth import is_authorized


class AuthTests(unittest.TestCase):
    def test_allowed_user_is_authorized(self):
        self.assertTrue(is_authorized(123, {123}))

    def test_missing_or_unknown_user_is_rejected(self):
        self.assertFalse(is_authorized(None, {123}))
        self.assertFalse(is_authorized(456, {123}))


if __name__ == "__main__":
    unittest.main()
