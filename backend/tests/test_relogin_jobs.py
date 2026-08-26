import threading
import time
import unittest
from unittest import mock

from backend.registration import engine
from backend.web.relogin_jobs import (
    ReloginJobCoordinator,
    enqueue_relogin_grokiq_notification,
)


class _Store:
    def __init__(self, records):
        self.records = records

    def get_results_by_ids(self, ids):
        by_id = {record["id"]: record for record in self.records}
        return [by_id[account_id] for account_id in ids if account_id in by_id]


class ReloginJobCoordinatorTests(unittest.TestCase):
    def test_batch_preserves_order_and_counts_every_requested_account(self):
        store = _Store(
            [
                {"id": 1, "email": "one@example.com", "password": "secret"},
                {"id": 2, "email": "two@example.com", "password": ""},
                {"id": 3, "email": "three@example.com", "password": "secret"},
                {"id": 4, "email": "four@example.com", "password": "secret"},
            ]
        )
        coordinator = ReloginJobCoordinator()
        processed = []

        def run_record(record, _store):
            processed.append(record["id"])
            if record["id"] == 3:
                raise RuntimeError("fixture failure")
            return ""

        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_run_record", side_effect=run_record),
        ):
            coordinator.start_many([4, 1, 2, 3, 99, 1])
            deadline = time.time() + 2
            while coordinator.status()["running"] and time.time() < deadline:
                time.sleep(0.01)

        status = coordinator.status()
        self.assertFalse(status["running"])
        self.assertEqual(processed, [4, 1, 3])
        self.assertEqual(status["total_count"], 5)
        self.assertEqual(status["completed_count"], 5)
        self.assertEqual(status["success_count"], 2)
        self.assertEqual(status["failed_count"], 3)
        self.assertEqual(status["error"], "3 个账号重新登录失败")
        # items 按请求顺序，processed 按执行顺序，两者刻意不同。
        self.assertEqual(
            [(item["account_id"], item["status"], item["error"]) for item in status["items"]],
            [
                (4, "success", ""),
                (1, "success", ""),
                (2, "failed", "没有保存密码"),
                (3, "failed", "fixture failure"),
                (99, "failed", "记录不存在"),
            ],
        )
        self.assertEqual(len(status["items"]), status["total_count"])
        self.assertTrue(status["run_id"])

    def test_single_missing_account_keeps_not_found_contract(self):
        coordinator = ReloginJobCoordinator()
        with mock.patch.object(
            engine,
            "get_registration_repository",
            return_value=_Store([]),
        ):
            with self.assertRaisesRegex(LookupError, "记录不存在"):
                coordinator.start(7)

    def test_thread_start_failure_releases_running_state(self):
        coordinator = ReloginJobCoordinator()
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch("backend.web.relogin_jobs.threading.Thread.start", side_effect=RuntimeError("start failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                coordinator.start(1)
        status = coordinator.status()
        self.assertFalse(status["running"])
        self.assertTrue(all(item["status"] == "failed" for item in status["items"]))

    def _wait_idle(self, coordinator, timeout=2):
        deadline = time.time() + timeout
        while coordinator.status()["running"] and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(coordinator.status()["running"], "任务未在超时前结束")

    def _run_single(self, coordinator, store, error=""):
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_run_record", return_value=error),
        ):
            coordinator.start(1)
            self._wait_idle(coordinator)
        return coordinator.status()

    def test_single_success_reports_one_item(self):
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        status = self._run_single(ReloginJobCoordinator(), store)
        self.assertEqual(status["total_count"], 1)
        self.assertEqual(status["stage"], "重新登录完成")
        self.assertEqual(status["error"], "")
        self.assertEqual(
            status["items"],
            [{"account_id": 1, "email": "one@example.com", "status": "success", "error": ""}],
        )

    def test_single_failure_keeps_raw_error_on_scalar_and_item(self):
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        status = self._run_single(ReloginJobCoordinator(), store, error="登录超时")
        self.assertEqual(status["stage"], "重新登录失败")
        # 原实现用 errors[0].split(": ", 1)[-1] 还原原始错误，改由 items 派生后必须等价。
        self.assertEqual(status["error"], "登录超时")
        self.assertEqual(status["items"][0]["status"], "failed")
        self.assertEqual(status["items"][0]["error"], "登录超时")

    def test_failure_report_keeps_structured_browser_diagnostics(self):
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        coordinator = ReloginJobCoordinator()
        outcome = {
            "error": "登录失败: 凭据无效",
            "stage": "填写邮箱和密码",
            "error_type": "RuntimeError",
            "url": "https://accounts.example/sign-in",
            "visible_error": "凭据无效",
            "controls": "input[email] | button: 下一步",
            "page_text": "邮箱 凭据无效",
            "screenshot_url": "/api/accounts/1/failure-screenshot",
            "traceback": "RuntimeError: 登录失败: 凭据无效",
        }
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_run_record", return_value=outcome),
        ):
            coordinator.start(1)
            self._wait_idle(coordinator)
        item = coordinator.status()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["stage"], "填写邮箱和密码")
        self.assertEqual(item["error_type"], "RuntimeError")
        self.assertEqual(item["visible_error"], "凭据无效")
        self.assertTrue(item["screenshot_url"].endswith("failure-screenshot"))

    def test_status_items_are_defensive_copies(self):
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        coordinator = ReloginJobCoordinator()
        self._run_single(coordinator, store)

        leaked = coordinator.status()["items"]
        leaked.append({"account_id": 999})
        leaked[0]["status"] = "tampered"

        items = coordinator.status()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "success")

    def test_run_id_is_fresh_per_run_and_items_reset(self):
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        coordinator = ReloginJobCoordinator()
        first = self._run_single(coordinator, store)["run_id"]
        second = self._run_single(coordinator, store)["run_id"]
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)
        self.assertEqual(len(coordinator.status()["items"]), 1)

    def test_items_update_incrementally_while_running(self):
        store = _Store(
            [
                {"id": 1, "email": "one@example.com", "password": "secret"},
                {"id": 2, "email": "two@example.com", "password": "secret"},
            ]
        )
        coordinator = ReloginJobCoordinator()
        gate = threading.Event()

        def run_record(record, _store):
            if record["id"] == 2:
                gate.wait(2)
            return ""

        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
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
                self.assertEqual(snapshot["items"][0]["status"], "success")
                self.assertEqual(snapshot["items"][1]["status"], "pending")
            finally:
                gate.set()
                self._wait_idle(coordinator)

        self.assertEqual(coordinator.status()["success_count"], 2)

    def test_success_item_includes_sso_check_result(self):
        store = _Store([{"id": 1, "email": "one@example.com", "password": "secret"}])
        coordinator = ReloginJobCoordinator()
        outcome = {
            "error": "",
            "sso_check_status": "clean",
            "sso_check_verdict": "clean",
            "bot_flag_source": 0,
            "sso_checked_at": "2026-08-14T00:00:00+00:00",
            "sso_check_attempts": 1,
        }
        with (
            mock.patch.object(engine, "get_registration_repository", return_value=store),
            mock.patch.object(coordinator, "_run_record", return_value=outcome),
        ):
            coordinator.start(1)
            self._wait_idle(coordinator)

        item = coordinator.status()["items"][0]
        self.assertEqual(item["status"], "success")
        self.assertEqual(item["sso_check_status"], "clean")
        self.assertEqual(item["bot_flag_source"], 0)
        self.assertEqual(item["sso_check_attempts"], 1)

    def test_successful_relogin_enqueues_grokiq_webhook_after_grok_build_import(self):
        store = mock.Mock()
        store.get_results_by_ids.return_value = [
            {"id": 7, "email": "ok@example.com", "bot_risk": False, "bfs": "0"}
        ]
        event = {"event_id": "registration:7:grok2api-imported"}
        config = {"grokiq_webhook_enabled": True}
        logs = []

        with mock.patch(
            "backend.integrations.grokiq.enqueue_imported_account",
            return_value=event,
        ) as enqueue:
            queued = enqueue_relogin_grokiq_notification(
                store,
                7,
                {
                    "grok2api_remote_result": {
                        "formats": {"grok_build": {"created": 1}},
                        "errors": {},
                    }
                },
                config,
                sso="REFRESHED-SSO",
                log_callback=logs.append,
            )

        self.assertEqual(queued, event)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1]["email"], "ok@example.com")
        self.assertEqual(enqueue.call_args.args[1]["sso"], "REFRESHED-SSO")
        self.assertTrue(any("已加入联动通知队列" in item for item in logs))

    def test_relogin_skips_grokiq_webhook_when_grok_build_was_not_imported(self):
        store = mock.Mock()
        with mock.patch(
            "backend.integrations.grokiq.enqueue_imported_account"
        ) as enqueue:
            queued = enqueue_relogin_grokiq_notification(
                store,
                7,
                {"grok2api_remote_result": {"formats": {}, "errors": {}}},
                {"grokiq_webhook_enabled": True},
            )

        self.assertIsNone(queued)
        store.get_results_by_ids.assert_not_called()
        enqueue.assert_not_called()

    def test_run_record_enqueues_webhook_after_successful_rebuild(self):
        import tempfile
        from pathlib import Path

        store = mock.Mock()
        store.update_relogin_result.return_value = True
        coordinator = ReloginJobCoordinator()
        record = {"id": 7, "email": "ok@example.com", "password": "secret"}
        cpa_detail = {
            "status": "success",
            "grok2api_remote_result": {"formats": {"grok_build": {"created": 1}}},
        }
        risk_state = {
            "mode": "relogin_detailed",
            "verdict": "clean",
            "bot_flag_source": 0,
            "bot_flag": {"source": 0, "found": True},
        }
        risk_compact = {
            "status": "clean",
            "verdict": "clean",
            "bot_flag_source": 0,
            "checked_at": "2026-08-14T00:00:00+00:00",
            "attempts": 1,
            "error": "",
        }

        with tempfile.TemporaryDirectory() as tmp:
            account_file = str(Path(tmp) / "ok@example.com.txt")

            def capture_detail(sso, email="", log_callback=None, result_out=None):
                result_out.update(cpa_detail)
                return True

            with (
                mock.patch.object(engine, "load_config"),
                mock.patch.object(engine, "_wire_runtime_modules"),
                mock.patch.object(engine._bs, "allow_browser_launches"),
                mock.patch.object(engine, "account_file_for_email", return_value=account_file),
                mock.patch.object(engine, "add_sso_to_cpa", side_effect=capture_detail),
                mock.patch(
                    "backend.web.sso_check_jobs.inspect_sso_token",
                    return_value=(risk_state, risk_compact),
                ) as inspect_sso,
                mock.patch(
                    "backend.registration.login_flow.login_with_password",
                    return_value="sso",
                ),
                mock.patch("backend.automation.session.stop_browser"),
                mock.patch(
                    "backend.web.relogin_jobs.enqueue_relogin_grokiq_notification",
                    return_value={"event_id": "registration:7:grok2api-imported"},
                ) as enqueue,
            ):
                outcome = coordinator._run_record(record, store)

        self.assertEqual(outcome["error"], "")
        self.assertEqual(outcome["sso_check_status"], "clean")
        self.assertEqual(outcome["bot_flag_source"], 0)
        inspect_sso.assert_called_once()
        self.assertEqual(inspect_sso.call_args.args[:2], ("sso", "ok@example.com"))
        self.assertEqual(inspect_sso.call_args.kwargs["mode"], "relogin_detailed")
        store.update_relogin_result.assert_called_once()
        self.assertEqual(store.update_relogin_result.call_args.kwargs["status"], "success")
        store.update_sso_check_result.assert_called_once_with(
            7,
            risk_state=risk_state,
            status="clean",
        )
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], 7)
        self.assertEqual(
            enqueue.call_args.args[2]["grok2api_remote_result"]["formats"]["grok_build"],
            {"created": 1},
        )

    def test_flagged_sso_check_stops_auth_rebuild_and_persists_risk(self):
        import tempfile
        from pathlib import Path

        store = mock.Mock()
        store.update_relogin_result.return_value = True
        store.update_sso_check_result.return_value = True
        coordinator = ReloginJobCoordinator()
        record = {
            "id": 9,
            "email": "risk@example.com",
            "password": "secret",
            "cpa_enabled": True,
            "cpa_status": "success",
            "cpa_auth_path": "/old/cpa.json",
            "grok2api_auth_path": "/old/grok2api.json",
            "bot_risk": False,
            "bfs": "0",
        }
        risk_state = {
            "mode": "relogin_detailed",
            "verdict": "flagged",
            "bot_flag_source": 3,
            "bot_flag_details": "policy=test,event=fixture",
            "bot_flag": {"source": 3, "found": True, "flagged": True},
        }
        risk_compact = {
            "status": "flagged",
            "verdict": "flagged",
            "bot_flag_source": 3,
            "checked_at": "2026-08-14T00:00:00+00:00",
            "attempts": 1,
            "error": "",
        }

        with tempfile.TemporaryDirectory() as tmp:
            account_file = str(Path(tmp) / "risk@example.com.txt")
            with (
                mock.patch.object(engine, "load_config"),
                mock.patch.object(engine, "_wire_runtime_modules"),
                mock.patch.object(engine._bs, "allow_browser_launches"),
                mock.patch.object(engine, "account_file_for_email", return_value=account_file),
                mock.patch.object(engine, "add_sso_to_cpa") as add_sso,
                mock.patch(
                    "backend.web.sso_check_jobs.inspect_sso_token",
                    return_value=(risk_state, risk_compact),
                ),
                mock.patch(
                    "backend.registration.login_flow.login_with_password",
                    return_value="new-sso",
                ),
                mock.patch(
                    "backend.registration.login_flow.capture_login_diagnostics",
                    return_value={},
                ),
                mock.patch(
                    "backend.registration.login_flow.capture_login_failure",
                    return_value="",
                ),
                mock.patch("backend.automation.session.stop_browser"),
                mock.patch(
                    "backend.web.relogin_jobs.enqueue_relogin_grokiq_notification"
                ) as enqueue,
            ):
                outcome = coordinator._run_record(record, store)

        self.assertIn("SSO 风控异常", outcome["error"])
        self.assertEqual(outcome["sso_check_status"], "flagged")
        self.assertEqual(outcome["bot_flag_source"], 3)
        add_sso.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(store.update_sso_check_result.call_count, 2)
        self.assertTrue(
            all(
                call == mock.call(9, risk_state=risk_state, status="flagged")
                for call in store.update_sso_check_result.call_args_list
            )
        )
        store.update_relogin_result.assert_called_once()
        saved = store.update_relogin_result.call_args.kwargs
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["failure_type"], "registration_risk")
        self.assertIn("SSO 风控异常", saved["failure_reason"])
        self.assertTrue(saved["cpa_detail"]["bot_risk"])
        self.assertEqual(saved["cpa_detail"]["bfs"], 3)
        self.assertEqual(saved["cpa_detail"]["cpa_auth_path"], "/old/cpa.json")
        self.assertEqual(
            saved["cpa_detail"]["grok2api_auth_path"],
            "/old/grok2api.json",
        )


if __name__ == "__main__":
    unittest.main()
