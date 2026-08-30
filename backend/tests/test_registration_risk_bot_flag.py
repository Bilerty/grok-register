"""注册风控拒绝要和 access_token bfs 非 0 一样标记 bot_risk。

botFlagSource 与 access_token 里的 bfs 声明是同一个字段，只是一个来自注册后的
账号状态页、一个来自换到的 token。之前 FAIL_RISK 只写了 cpa_status=rejected，
没写 bot_risk，导致被风控的账号在前端显示成普通邮件图标而不是盾牌。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registration import engine
from backend.registration.store import RegistrationRepository


class RegistrationRiskBotFlagTests(unittest.TestCase):
    def test_exception_carries_verdict_and_source(self):
        exc = engine.RegistrationRiskDenied(
            "注册风控拒绝",
            bot_risk=True,
            bot_flag_source=1,
            bot_flag_details="policy=deny,risk=0.96,event=$registration",
        )
        self.assertTrue(exc.bot_risk)
        self.assertEqual(exc.bot_flag_source, 1)
        self.assertIn("policy=deny", exc.bot_flag_details)

    def test_precondition_failure_is_not_a_bot_flag(self):
        # "sso 为空" 是前置条件失败，不是服务端裁决，不能标 bot_risk。
        exc = engine.RegistrationRiskDenied("注册风控检查失败: sso 为空")
        self.assertFalse(exc.bot_risk)
        detail = {}
        self.assertFalse(engine.apply_risk_bot_flag(detail, exc))
        self.assertNotIn("bot_risk", detail)

    def test_apply_sets_bot_risk_and_bfs(self):
        exc = engine.RegistrationRiskDenied(
            "注册风控拒绝", bot_risk=True, bot_flag_source=1
        )
        detail = {"status": "rejected"}
        self.assertTrue(engine.apply_risk_bot_flag(detail, exc))
        self.assertTrue(detail["bot_risk"])
        self.assertEqual(detail["bfs"], 1)

    def test_apply_handles_none_source(self):
        exc = engine.RegistrationRiskDenied(
            "注册风控拒绝", bot_risk=True, bot_flag_source=None
        )
        detail = {}
        self.assertTrue(engine.apply_risk_bot_flag(detail, exc))
        self.assertTrue(detail["bot_risk"])
        self.assertEqual(detail["bfs"], "")

    def test_apply_ignores_unrelated_exception(self):
        detail = {}
        self.assertFalse(engine.apply_risk_bot_flag(detail, ValueError("boom")))
        self.assertEqual(detail, {})

    def test_apply_tolerates_non_dict_detail(self):
        exc = engine.RegistrationRiskDenied("x", bot_risk=True, bot_flag_source=1)
        self.assertFalse(engine.apply_risk_bot_flag(None, exc))

    def test_risk_denial_raise_site_sets_verdict(self):
        denied = {
            "found": True,
            "bot_flag_source": 1,
            "bot_flag_details": "policy=deny,risk=0.96,event=$registration",
            "policy": "deny",
            "denied": True,
            "error": "",
        }
        original = dict(engine.config)
        engine.config.update(
            {
                "cpa_auto_add": True,
                "cpa_auth_dir": "data/cpa_auth",
                "sso_detailed_risk_check": False,
                "cpa_registration_risk_check": True,
            }
        )
        try:
            with (
                mock.patch.object(
                    engine._s2cpa, "inspect_sso_account_state", return_value=denied
                ),
                mock.patch.object(engine, "_append_sso_risk_rejected"),
            ):
                with self.assertRaises(engine.RegistrationRiskDenied) as ctx:
                    engine.ensure_sso_oauth_eligible("sso", email="a@b.com")
        finally:
            engine.config.clear()
            engine.config.update(original)

        self.assertTrue(ctx.exception.bot_risk)
        self.assertEqual(ctx.exception.bot_flag_source, 1)
        self.assertEqual(engine.classify_failure(ctx.exception), engine.FAIL_RISK)

    def test_persisted_risk_failure_row_has_bot_risk(self):
        """端到端：风控拒绝的失败记录落库后 bot_risk=1，前端才会显示盾牌。"""
        exc = engine.RegistrationRiskDenied(
            "注册风控拒绝，已跳过 OAuth: botFlagSource=1",
            bot_risk=True,
            bot_flag_source=1,
        )
        cpa_detail = {"enabled": True}
        cpa_detail.update(status="rejected", error=str(exc))
        engine.apply_risk_bot_flag(cpa_detail, exc)

        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ):
                row_id = engine.persist_registration_result(
                    batch_id="b1",
                    source="web",
                    started_at=0,
                    email="risk@example.com",
                    status="failure",
                    cpa_detail=cpa_detail,
                    failure_type=engine.FAIL_RISK,
                    failure_reason=str(exc),
                )
            self.assertIsNotNone(row_id)
            row = store.get_results_by_ids([row_id])[0]
            self.assertEqual(row["failure_type"], "registration_risk")
            self.assertEqual(row["bot_risk"], 1)
            self.assertEqual(str(row["bfs"]), "1")

    def test_backfill_marks_historical_risk_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            verdict_id = store.add_result(
                {
                    "email": "old-risk@example.com",
                    "status": "failure",
                    "failure_type": "registration_risk",
                    "failure_reason": "注册风控拒绝，已跳过 OAuth: botFlagSource=1 policy=deny",
                }
            )
            precondition_id = store.add_result(
                {
                    "email": "no-sso@example.com",
                    "status": "failure",
                    "failure_type": "registration_risk",
                    "failure_reason": "注册风控检查失败: sso 为空",
                }
            )
            other_id = store.add_result(
                {
                    "email": "timeout@example.com",
                    "status": "failure",
                    "failure_type": "code_timeout",
                    "failure_reason": "未收到验证码",
                }
            )

            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ):
                updated = engine.backfill_registration_risk_bot_risk()

            self.assertEqual(updated, 1)
            self.assertEqual(store.get_results_by_ids([verdict_id])[0]["bot_risk"], 1)
            # 前置条件失败与其它失败类型都不能被误标
            self.assertEqual(store.get_results_by_ids([precondition_id])[0]["bot_risk"], 0)
            self.assertEqual(store.get_results_by_ids([other_id])[0]["bot_risk"], 0)

    def test_backfill_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.add_result(
                {
                    "email": "old-risk@example.com",
                    "status": "failure",
                    "failure_type": "registration_risk",
                    "failure_reason": "botFlagSource=1",
                }
            )
            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ):
                self.assertEqual(engine.backfill_registration_risk_bot_risk(), 1)
                self.assertEqual(engine.backfill_registration_risk_bot_risk(), 0)

    def test_backfill_survives_repository_failure(self):
        with mock.patch.object(
            engine, "get_registration_repository", side_effect=RuntimeError("no db")
        ):
            self.assertEqual(engine.backfill_registration_risk_bot_risk(), 0)

    def test_persisted_non_risk_failure_stays_unflagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            with mock.patch.object(
                engine, "get_registration_repository", return_value=store
            ):
                row_id = engine.persist_registration_result(
                    batch_id="b1",
                    source="web",
                    started_at=0,
                    email="timeout@example.com",
                    status="failure",
                    cpa_detail={"enabled": True},
                    failure_type=engine.FAIL_CODE,
                    failure_reason="未收到验证码",
                )
            row = store.get_results_by_ids([row_id])[0]
            self.assertEqual(row["bot_risk"], 0)


if __name__ == "__main__":
    unittest.main()
