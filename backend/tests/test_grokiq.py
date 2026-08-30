import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.integrations.grokiq import GrokIQNotifier, enqueue_imported_account
from backend.registration.store import RegistrationRepository


class FakeResponse:
    def __init__(self, status_code=202, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return None


class FakeSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class GrokIQOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RegistrationRepository(Path(self.tmp.name) / "results.sqlite3")
        self.registration_id = self.store.add_result(
            {"email": "grokiq@example.com", "status": "success"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_event_is_idempotent_and_tracks_delivery_lifecycle(self):
        event = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="GrokIQ@Example.com",
            bot_risk=True,
            bfs=1,
            occurred_at="2026-08-11T12:00:00Z",
        )
        duplicate = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs=0,
            occurred_at="2026-08-11T12:01:00Z",
        )

        self.assertEqual(event["event_id"], duplicate["event_id"])
        self.assertEqual(event["email"], "grokiq@example.com")
        claimed = self.store.claim_grokiq_delivery()
        self.assertEqual(claimed["status"], "delivering")
        self.assertEqual(claimed["attempts"], 1)

        self.store.complete_grokiq_delivery(claimed["event_id"])
        delivery = self.store.grokiq_deliveries([self.registration_id])[self.registration_id]
        self.assertEqual(delivery["status"], "delivered")
        self.assertEqual(delivery["attempts"], 1)
        self.assertTrue(delivery["delivered_at"])

    def test_failed_webhook_is_returned_to_outbox_with_backoff(self):
        event = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs="",
            occurred_at="2026-08-11T12:00:00Z",
        )
        claimed = self.store.claim_grokiq_delivery()
        notifier = GrokIQNotifier()
        notifier._repository = self.store
        session = FakeSession(FakeResponse(status_code=503, text="unavailable"))

        with mock.patch(
            "backend.integrations.grokiq.requests.Session",
            return_value=session,
        ):
            notifier._deliver(
                claimed,
                {
                    "url": "http://grokiq.test/account-imported",
                    "token": "shared-token",
                    "timeout": 10,
                },
            )

        delivery = self.store.grokiq_deliveries([self.registration_id])[self.registration_id]
        self.assertEqual(delivery["event_id"], event["event_id"])
        self.assertEqual(delivery["status"], "pending")
        self.assertEqual(delivery["attempts"], 1)
        self.assertIn("HTTP 503", delivery["last_error"])
        self.assertGreater(delivery["next_attempt_at"], time.time())

    def test_successful_webhook_marks_claimed_event_delivered(self):
        event = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs="",
            occurred_at="2026-08-11T12:00:00Z",
            sso="RAW-SSO",
        )
        claimed = self.store.claim_grokiq_delivery()
        session = FakeSession()
        notifier = GrokIQNotifier()
        notifier._repository = self.store

        with mock.patch(
            "backend.integrations.grokiq.requests.Session",
            return_value=session,
        ):
            notifier._deliver(
                claimed,
                {
                    "url": "http://grokiq.test/account-imported",
                    "token": "shared-token",
                    "timeout": 10,
                },
            )

        self.assertEqual(len(session.calls), 1)
        url, request = session.calls[0]
        self.assertEqual(url, "http://grokiq.test/account-imported")
        self.assertEqual(request["headers"]["x-grokiq-token"], "shared-token")
        self.assertEqual(request["json"]["event_id"], event["event_id"])
        self.assertEqual(request["json"]["sso"], "RAW-SSO")
        delivery = self.store.grokiq_deliveries([self.registration_id])[self.registration_id]
        self.assertEqual(delivery["status"], "delivered")

    def test_non_empty_sso_refreshes_a_delivered_outbox_event(self):
        event = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs="",
            occurred_at="2026-08-11T12:00:00Z",
        )
        claimed = self.store.claim_grokiq_delivery()
        self.store.complete_grokiq_delivery(claimed["event_id"])

        refreshed = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs="",
            occurred_at="2026-08-11T12:05:00Z",
            sso="REFRESHED-SSO",
        )

        self.assertEqual(refreshed["event_id"], event["event_id"])
        self.assertEqual(refreshed["status"], "pending")
        self.assertEqual(refreshed["sso"], "REFRESHED-SSO")

    def test_same_sso_keeps_a_delivered_outbox_event_idempotent(self):
        event = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs="",
            occurred_at="2026-08-11T12:00:00Z",
            sso="SAME-SSO",
        )
        claimed = self.store.claim_grokiq_delivery()
        self.store.complete_grokiq_delivery(claimed["event_id"])

        duplicate = self.store.enqueue_grokiq_event(
            registration_id=self.registration_id,
            email="grokiq@example.com",
            bot_risk=False,
            bfs="",
            occurred_at="2026-08-11T12:05:00Z",
            sso="SAME-SSO",
        )

        self.assertEqual(duplicate["event_id"], event["event_id"])
        self.assertEqual(duplicate["status"], "delivered")

    def test_enqueue_imported_account_reads_sso_from_account_file(self):
        account_file = Path(self.tmp.name) / "grokiq@example.com.txt"
        account_file.write_text(
            "grokiq@example.com----password----FILE-SSO\n",
            encoding="utf-8",
        )

        event = enqueue_imported_account(
            self.store,
            {
                "id": self.registration_id,
                "email": "grokiq@example.com",
                "account_file": str(account_file),
            },
            {"grokiq_webhook_enabled": True},
        )

        self.assertEqual(event["sso"], "FILE-SSO")

    def test_invalid_account_file_is_not_sent_as_sso(self):
        account_file = Path(self.tmp.name) / "grokiq@example.com.txt"
        account_file.write_text("invalid-account-file\n", encoding="utf-8")

        event = enqueue_imported_account(
            self.store,
            {
                "id": self.registration_id,
                "email": "grokiq@example.com",
                "account_file": str(account_file),
            },
            {"grokiq_webhook_enabled": True},
        )

        self.assertEqual(event["sso"], "")

    def test_grokiq_result_matches_registration_id_then_email(self):
        saved = self.store.save_grokiq_result(
            {
                "event_id": "registration:1:grok2api-imported",
                "registration_id": str(self.registration_id),
                "email": "other@example.com",
                "degraded": True,
                "verdict": "degraded",
            }
        )
        self.assertIsNotNone(saved)
        self.assertEqual(int(saved["id"]), self.registration_id)
        extra = self.store.get_results_by_ids([self.registration_id])[0]
        payload = extra["extra_json"]
        stored = json.loads(payload)
        self.assertTrue(stored["grokiq_result"]["degraded"])
        self.assertEqual(stored["grokiq_result"]["verdict"], "degraded")
        self.assertEqual(int(saved["bot_risk"] or 0), 1)

        other_id = self.store.add_result(
            {"email": "callback@example.com", "status": "success"}
        )
        matched = self.store.save_grokiq_result(
            {
                "registration_id": "not-a-number",
                "email": "CALLBACK@example.com",
                "degraded": False,
                "verdict": "normal",
            }
        )
        self.assertIsNotNone(matched)
        self.assertEqual(int(matched["id"]), other_id)
        self.assertEqual(int(matched["bot_risk"] or 0), 0)

        missing = self.store.save_grokiq_result(
            {
                "registration_id": "",
                "email": "missing@example.com",
                "degraded": True,
            }
        )
        self.assertIsNone(missing)


if __name__ == "__main__":
    unittest.main()
