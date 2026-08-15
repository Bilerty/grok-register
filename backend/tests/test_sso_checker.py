import unittest

from backend.integrations.sso_checker import (
    BotFlagInfo,
    SsoCheckConfig,
    SsoChecker,
    SsoCredential,
    SsoVerdict,
)


class _Cookies:
    def __init__(self):
        self.set_calls = []

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return None


class _Response:
    url = "https://grok.com/"

    def __init__(self, source, status_code=200):
        self.status_code = status_code
        source_json = "null" if source is None else str(source)
        self.text = (
            '<script>self.__next_f.push([1,"'
            '\\"initialData\\":{\\"user\\":{'
            '\\"email\\":\\"fixture@example.com\\",'
            '\\"userId\\":\\"fixture-user\\",'
            f'\\"botFlagSource\\":{source_json}'
            '}}"])</script>'
        )


class _Session:
    def __init__(self, source, status_code=200):
        self.cookies = _Cookies()
        self.source = source
        self.status_code = status_code
        self.proxies = {}
        self.get_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return _Response(self.source, self.status_code)


class SsoCheckerTests(unittest.TestCase):
    def test_bot_flag_zero_is_clean_and_every_nonzero_value_is_flagged(self):
        self.assertFalse(BotFlagInfo(found=True, source=0).is_flagged())
        self.assertFalse(BotFlagInfo(found=True, source=0, denied=True).is_flagged())
        for source in (1, 2, 3, -1):
            with self.subTest(source=source):
                self.assertTrue(BotFlagInfo(found=True, source=source).is_flagged())

    def test_null_source_is_invalid_or_unknown_and_token_is_not_serialized(self):
        token = "fixture-sensitive-sso-token"
        checker = SsoChecker(session_factory=lambda: _Session(None))
        result = checker.check(
            SsoCredential(token, expected_email="fixture@example.com")
        )
        serialized = result.to_dict()

        self.assertEqual(result.verdict, SsoVerdict.INVALID_OR_UNKNOWN)
        self.assertTrue(result.valid_session)
        self.assertIsNone(result.bot_flag.source)
        self.assertNotIn(token, repr(result))
        self.assertNotIn(token, str(serialized))

    def test_zero_source_is_clean(self):
        checker = SsoChecker(session_factory=lambda: _Session(0))
        result = checker.check(
            SsoCredential("fixture-token", expected_email="fixture@example.com")
        )
        self.assertEqual(result.verdict, SsoVerdict.CLEAN)

    def test_nonzero_source_is_flagged(self):
        for source in (1, 2, 3, -1):
            with self.subTest(source=source):
                checker = SsoChecker(session_factory=lambda source=source: _Session(source))
                result = checker.check(SsoCredential("fixture-token"))
                self.assertEqual(result.verdict, SsoVerdict.FLAGGED)

    def test_edge_error_retries_with_impersonation_matched_user_agent(self):
        first_session = _Session(0, 403)
        second_session = _Session(0, 200)
        sessions = iter((first_session, second_session))

        def session_factory():
            return next(sessions)

        checker = SsoChecker(
            config=SsoCheckConfig(proxy="http://proxy.test:8080"),
            session_factory=session_factory,
        )
        result = checker.check(SsoCredential("fixture-token"))

        self.assertEqual(result.verdict, SsoVerdict.CLEAN)
        self.assertTrue(result.metadata["edge_header_retry"])
        self.assertIn("User-Agent", first_session.get_calls[0][1]["headers"])
        self.assertNotIn("User-Agent", second_session.get_calls[0][1]["headers"])
        self.assertEqual(
            first_session.get_calls[0][1]["headers"]["Accept-Language"],
            "en-US,en;q=0.9",
        )
        for session in (first_session, second_session):
            self.assertEqual(
                session.proxies,
                {
                    "http": "http://proxy.test:8080",
                    "https": "http://proxy.test:8080",
                },
            )
            self.assertEqual(len(session.cookies.set_calls), 10)


if __name__ == "__main__":
    unittest.main()
