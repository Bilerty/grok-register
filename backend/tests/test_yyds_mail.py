import inspect
import unittest

from backend.mailbox import yyds_mail


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class YydsMailTests(unittest.TestCase):
    def test_wait_for_code_reads_numeric_hyphenated_subject(self):
        address = "fixture@example.com"

        def http_get(url, **kwargs):
            if url.endswith("/messages"):
                return _Response(
                    {
                        "success": True,
                        "data": {
                            "messages": [
                                {"id": "message-1", "to": [{"address": address}]}
                            ]
                        },
                    }
                )
            return _Response(
                {
                    "success": True,
                    "data": {
                        "subject": "SpaceXAI confirmation code: 134-771",
                        "text": "",
                        "html": [],
                    },
                }
            )

        code = yyds_mail.wait_for_code(
            http_get,
            "fixture-token",
            address,
            timeout=1,
            raise_if_cancelled=lambda callback: None,
            sleep_with_cancel=lambda seconds, callback: None,
        )

        self.assertEqual(code, "134-771")

    def test_wait_for_code_default_timeout_is_60_seconds(self):
        default = inspect.signature(yyds_mail.wait_for_code).parameters["timeout"].default
        self.assertEqual(default, 60)


if __name__ == "__main__":
    unittest.main()
