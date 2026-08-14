import json
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from backend.registration import engine
from backend.registration.store import RegistrationRepository
from backend.web.sso_check_jobs import SsoCheckJobCoordinator


class _Store:
    def __init__(self, records):
        self.records = records
        self.saved = []

    def get_results_by_ids(self, ids):
        by_id = {record["id"]: record for record in self.records}
        return [by_id[account_id] for account_id in ids if account_id in by_id]

    def update_sso_check_result(self, account_id, *, risk_state, status):
        self.saved.append((account_id, status, risk_state))
        return True


class SsoCheckJobCoordinatorTests(unittest.TestCase):
    def _wait_idle(self, coordinator, timeout=2):
        deadline = time.time() + timeout
        while coordinator.status()["running"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(coordinator.status()["running"], "任务未在超时前结束")

    def test_batch_preserves_order_and_counts_risk_verdicts(self):
        store = _Store(
            [
                {"id": 1, "email": "one@example.com", "account_file": "/fixture/1.txt"},
                {"id": 2, "email": "two@example.com", "account_file": "/fixture/2.txt"},
                {"id": 3, "email": "three@example.com", "account_file": "/fixture/3.txt"},
            ]
        )
        coordinator = SsoCheckJobCoordinator()
        outcomes = {
            1: {"status": "clean", "verdict": "clean", "bot_flag_source": 0, "error": ""},
            2: {"status": "flagged", "verdict": "flagged", "bot_flag_source": 2, "error": ""},
            3: {"status": "unknown", "verdict": "invalid_or_unknown", "bot_flag_source": None, "error": "字段为空"},
        }
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_find_sso_file", return_value=Path(__file__)),
            mock.patch("backend.web.sso_check_jobs.read_sso_token", return_value="fixture-token"),
            mock.patch.object(coordinator, "_run_record", side_effect=lambda record, _store: outcomes[record["id"]]),
        ):
            coordinator.start_many([2, 1, 99, 3, 2])
            self._wait_idle(coordinator)

        status = coordinator.status()
        self.assertEqual(status["total_count"], 4)
        self.assertEqual(status["completed_count"], 4)
        self.assertEqual(status["clean_count"], 1)
        self.assertEqual(status["flagged_count"], 1)
        self.assertEqual(status["unknown_count"], 1)
        self.assertEqual(status["failed_count"], 1)
        self.assertEqual(
            [(item["account_id"], item["status"]) for item in status["items"]],
            [(2, "flagged"), (1, "clean"), (99, "failed"), (3, "unknown")],
        )

    def test_items_update_incrementally_while_running(self):
        store = _Store(
            [
                {"id": 1, "email": "one@example.com", "account_file": "/fixture/1.txt"},
                {"id": 2, "email": "two@example.com", "account_file": "/fixture/2.txt"},
            ]
        )
        coordinator = SsoCheckJobCoordinator()
        gate = threading.Event()

        def run_record(record, _store):
            if record["id"] == 2:
                gate.wait(2)
            return {"status": "clean", "verdict": "clean", "bot_flag_source": 0, "error": ""}

        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_find_sso_file", return_value=Path(__file__)),
            mock.patch("backend.web.sso_check_jobs.read_sso_token", return_value="fixture-token"),
            mock.patch.object(coordinator, "_run_record", side_effect=run_record),
        ):
            coordinator.start_many([1, 2])
            snapshot = coordinator.status()
            deadline = time.time() + 2
            while time.time() < deadline:
                snapshot = coordinator.status()
                if snapshot["items"][0]["status"] != "pending":
                    break
                time.sleep(0.01)
            try:
                self.assertTrue(snapshot["running"])
                self.assertEqual(snapshot["items"][0]["status"], "clean")
                self.assertEqual(snapshot["items"][1]["status"], "pending")
            finally:
                gate.set()
                self._wait_idle(coordinator)

    def test_thread_start_failure_counts_all_pending_items(self):
        store = _Store(
            [
                {"id": 1, "email": "one@example.com", "account_file": "/fixture/1.txt"},
                {"id": 2, "email": "two@example.com", "account_file": "/fixture/2.txt"},
            ]
        )
        coordinator = SsoCheckJobCoordinator()
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_find_sso_file", return_value=Path(__file__)),
            mock.patch("backend.web.sso_check_jobs.threading.Thread.start", side_effect=RuntimeError("thread failure")),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread failure"):
                coordinator.start_many([1, 2])

        status = coordinator.status()
        self.assertFalse(status["running"])
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["failed_count"], 2)
        self.assertEqual([item["status"] for item in status["items"]], ["failed", "failed"])

    def test_repository_persists_result_and_updates_bot_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            row_id = store.add_result({"email": "risk@example.com", "status": "success"})
            state = {
                "verdict": "flagged",
                "bot_flag_source": 3,
                "bot_flag": {"source": 3, "flagged": True},
            }
            self.assertTrue(store.update_sso_check_result(row_id, risk_state=state, status="flagged"))
            row = store.get_results_by_ids([row_id])[0]
            self.assertEqual(row["bot_risk"], 1)
            self.assertEqual(row["bfs"], "3")
            extra = json.loads(row["extra_json"])
            self.assertEqual(extra["sso_check_status"], "flagged")
            self.assertEqual(extra["sso_risk_check"]["bot_flag_source"], 3)

    def test_repository_unknown_result_keeps_previous_risk_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            row_id = store.add_result(
                {"email": "risk@example.com", "status": "success", "bot_risk": True, "bfs": "7"}
            )
            self.assertTrue(
                store.update_sso_check_result(
                    row_id,
                    risk_state={"verdict": "invalid_or_unknown", "bot_flag_source": None},
                    status="unknown",
                )
            )
            row = store.get_results_by_ids([row_id])[0]
            self.assertEqual(row["bot_risk"], 1)
            self.assertEqual(row["bfs"], "7")
            extra = json.loads(row["extra_json"])
            self.assertEqual(extra["sso_check_status"], "unknown")

    def test_repository_clean_result_without_source_keeps_existing_bfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            row_id = store.add_result(
                {"email": "clean@example.com", "status": "success", "bot_risk": False, "bfs": "7"}
            )
            self.assertTrue(
                store.update_sso_check_result(
                    row_id,
                    risk_state={"verdict": "clean", "bot_flag_source": None},
                    status="clean",
                )
            )
            row = store.get_results_by_ids([row_id])[0]
            self.assertEqual(row["bot_risk"], 0)
            self.assertEqual(row["bfs"], "7")
            self.assertEqual(json.loads(row["extra_json"])["sso_check_status"], "clean")

    def test_unknown_source_retries_then_persists_clean_result(self):
        @dataclass
        class _Result:
            source: object

            def to_dict(self, **_kwargs):
                source = self.source
                return {
                    "verdict": "clean" if source == 0 else "invalid_or_unknown",
                    "valid_session": True,
                    "email_match": True,
                    "checked_at": "2026-08-12T00:00:00Z",
                    "response_ms": 10,
                    "error": "" if source == 0 else "botFlagSource is missing or null",
                    "bot_flag": {
                        "found": source is not None,
                        "source": source,
                        "details": "",
                        "policy": "",
                        "risk": None,
                        "event": "",
                        "denied": False,
                    },
                }

        class _Checker:
            config = mock.Mock(flagged_sources=frozenset())

            def __init__(self, *_args, **_kwargs):
                self.results = iter([_Result(None), _Result(None), _Result(0)])

            def check(self, _credential):
                return next(self.results)

        store = _Store([{"id": 1, "email": "one@example.com", "account_file": "/fixture/1.txt"}])
        coordinator = SsoCheckJobCoordinator()
        with (
            mock.patch.object(coordinator, "_find_sso_file", return_value=Path(__file__)),
            mock.patch("backend.web.sso_check_jobs.read_sso_token", return_value="fixture-token"),
            mock.patch("backend.integrations.sso_checker.SsoChecker", _Checker),
            mock.patch("backend.web.sso_check_jobs.time.sleep") as sleep,
        ):
            result = coordinator._run_record(store.records[0], store)

        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["bot_flag_source"], 0)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual(store.saved[0][1], "clean")
        self.assertEqual(store.saved[0][2]["bot_flag_source"], 0)
        self.assertEqual(store.saved[0][2]["mode"], "batch_detailed")


if __name__ == "__main__":
    unittest.main()
