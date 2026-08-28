import unittest
from unittest import mock

from backend.integrations import auth_exchange
from backend.registration import engine


class RegistrationRiskTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(engine.config)
        engine.config.update(
            {
                "cpa_auto_add": True,
                "sso_detailed_risk_check": False,
                "cpa_registration_risk_check": True,
                "cpa_auth_dir": "data/cpa_auth",
                "cpa_remote_url": "",
                "grok2api_auth_dir": "",
                "proxy": "http://127.0.0.1:7897",
            }
        )

    def tearDown(self):
        engine.config.clear()
        engine.config.update(self.original_config)

    def test_parser_recognizes_registration_bot_flag(self):
        state = auth_exchange._parse_grok_account_state(
            r'{\"botFlagSource\":1,\"botFlagDetails\":'
            r'\"policy=deny,risk=0.96,event=$registration\"}'
        )

        self.assertTrue(state["found"])
        self.assertTrue(state["denied"])
        self.assertEqual(state["bot_flag_source"], 1)
        self.assertEqual(state["risk"], 0.96)

    def test_risk_check_retries_until_bot_flag_is_visible(self):
        unknown = {
            "found": False,
            "bot_flag_source": None,
            "bot_flag_details": "",
            "policy": "",
            "denied": False,
            "error": "grok.com HTTP 403",
        }
        denied = {
            "found": True,
            "bot_flag_source": 1,
            "bot_flag_details": "policy=deny,risk=0.96,event=$registration",
            "policy": "deny",
            "denied": True,
            "error": "",
        }
        with (
            mock.patch.object(
                engine._s2cpa,
                "inspect_sso_account_state",
                side_effect=[unknown, unknown, denied],
            ) as inspect,
            mock.patch.object(engine.time, "sleep") as sleep,
            mock.patch.object(engine, "_append_sso_risk_rejected") as append,
        ):
            with self.assertRaisesRegex(engine.RegistrationRiskDenied, "botFlagSource=1"):
                engine.ensure_sso_oauth_eligible("fixture-sso", email="fixture@example.com")

        self.assertEqual(inspect.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        append.assert_called_once()

    def test_risk_check_releases_repeated_unknown_state(self):
        unknown = {
            "found": False,
            "bot_flag_source": None,
            "bot_flag_details": "",
            "policy": "",
            "denied": False,
            "error": "grok.com HTTP 403",
        }
        with (
            mock.patch.object(
                engine._s2cpa,
                "inspect_sso_account_state",
                return_value=unknown,
            ) as inspect,
            mock.patch.object(engine.time, "sleep"),
        ):
            result = engine.ensure_sso_oauth_eligible("fixture-sso")

        self.assertIs(result, unknown)
        self.assertEqual(inspect.call_count, 4)

    def test_risk_check_accepts_explicit_zero_source(self):
        allowed = {
            "found": True,
            "bot_flag_source": 0,
            "bot_flag_details": "policy=deny,risk=0.96,event=$registration",
            "policy": "deny",
            "denied": True,
            "error": "",
        }
        with mock.patch.object(
            engine._s2cpa,
            "inspect_sso_account_state",
            return_value=allowed,
        ) as inspect:
            result = engine.ensure_sso_oauth_eligible("fixture-sso")

        self.assertIs(result, allowed)
        inspect.assert_called_once()

    def test_risk_check_rejects_every_nonzero_source(self):
        for source in (True, 1, 2, 3, -1, "2"):
            with self.subTest(source=source):
                state = {
                    "found": True,
                    "bot_flag_source": source,
                    "bot_flag_details": "",
                    "policy": "",
                    "denied": False,
                    "error": "",
                }
                with (
                    mock.patch.object(
                        engine._s2cpa,
                        "inspect_sso_account_state",
                        return_value=state,
                    ),
                    mock.patch.object(engine, "_append_sso_risk_rejected"),
                ):
                    with self.assertRaises(engine.RegistrationRiskDenied) as raised:
                        engine.ensure_sso_oauth_eligible("fixture-sso")
                self.assertEqual(raised.exception.bot_flag_source, source)

    def test_risk_check_retries_unparseable_source_as_unknown(self):
        unknown = {
            "found": True,
            "bot_flag_source": "unexpected",
            "bot_flag_details": "",
            "policy": "",
            "denied": False,
            "error": "",
        }
        with (
            mock.patch.object(
                engine._s2cpa,
                "inspect_sso_account_state",
                return_value=unknown,
            ) as inspect,
            mock.patch.object(engine.time, "sleep"),
        ):
            self.assertIs(engine.ensure_sso_oauth_eligible("fixture-sso"), unknown)
        self.assertEqual(inspect.call_count, 4)

    def test_detailed_check_retries_null_source_then_accepts_zero(self):
        unknown = {
            "valid_session": True,
            "found": True,
            "bot_flag_source": None,
            "bot_flag_details": "",
            "denied": False,
            "error": "botFlagSource is missing or null",
        }
        clean = {
            "valid_session": True,
            "found": True,
            "bot_flag_source": 0,
            "bot_flag_details": "",
            "denied": False,
            "error": "",
        }
        engine.config.update({"cpa_auto_add": False, "sso_detailed_risk_check": True})
        result_out = {}
        with (
            mock.patch.object(
                engine,
                "_inspect_sso_detailed_risk",
                side_effect=[unknown, unknown, clean],
            ) as inspect,
            mock.patch.object(engine.time, "sleep") as sleep,
        ):
            result = engine.ensure_sso_oauth_eligible(
                "fixture-sso",
                email="fixture@example.com",
                result_out=result_out,
            )

        self.assertIs(result, clean)
        self.assertEqual(inspect.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual(result_out["sso_risk_check"]["bot_flag_source"], 0)

    def test_detailed_check_keeps_null_source_unknown_after_all_retries(self):
        unknown = {
            "valid_session": True,
            "found": True,
            "bot_flag_source": None,
            "bot_flag_details": "",
            "verdict": "invalid_or_unknown",
            "denied": False,
            "error": "botFlagSource is missing or null",
        }
        engine.config.update({"cpa_auto_add": False, "sso_detailed_risk_check": True})
        with (
            mock.patch.object(
                engine,
                "_inspect_sso_detailed_risk",
                return_value=unknown,
            ) as inspect,
            mock.patch.object(engine.time, "sleep") as sleep,
        ):
            result = engine.ensure_sso_oauth_eligible("fixture-sso")

        self.assertIs(result, unknown)
        self.assertEqual(inspect.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8])

    def test_detailed_check_runs_with_cpa_disabled(self):
        clean = {
            "valid_session": True,
            "found": True,
            "bot_flag_source": 0,
            "bot_flag_details": "",
            "denied": False,
            "error": "",
        }
        engine.config.update({"cpa_auto_add": False, "sso_detailed_risk_check": True})
        with mock.patch.object(
            engine,
            "_inspect_sso_detailed_risk",
            return_value=clean,
        ) as inspect:
            self.assertIs(engine.ensure_sso_oauth_eligible("fixture-sso"), clean)
        inspect.assert_called_once()

    def test_disabled_checks_keep_existing_short_circuit(self):
        engine.config.update({"cpa_auto_add": False, "sso_detailed_risk_check": False})
        with (
            mock.patch.object(engine, "_inspect_sso_detailed_risk") as detailed,
            mock.patch.object(engine._s2cpa, "inspect_sso_account_state") as legacy,
        ):
            self.assertEqual(engine.ensure_sso_oauth_eligible("fixture-sso"), {})
        detailed.assert_not_called()
        legacy.assert_not_called()

    def test_registration_risk_check_is_skipped_by_default(self):
        engine.config["cpa_registration_risk_check"] = False
        with mock.patch.object(engine._s2cpa, "inspect_sso_account_state") as inspect:
            self.assertEqual(engine.ensure_sso_oauth_eligible("fixture-sso"), {})
        inspect.assert_not_called()

    def test_registration_risk_check_can_be_enabled(self):
        engine.config.update({"cpa_registration_risk_check": True})
        clean = {
            "found": True,
            "bot_flag_source": 0,
            "bot_flag_details": "",
            "policy": "",
            "denied": False,
            "error": "",
        }
        with mock.patch.object(
            engine._s2cpa, "inspect_sso_account_state", return_value=clean
        ) as inspect:
            self.assertIs(engine.ensure_sso_oauth_eligible("fixture-sso"), clean)
        inspect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
