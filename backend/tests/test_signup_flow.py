import unittest
from unittest import mock

from backend.registration import engine
from backend.registration import signup_flow


class SignupFlowTests(unittest.TestCase):
    class NativeInput:
        def __init__(self, current_value=""):
            self.current_value = current_value
            self.states = mock.Mock(is_alive=True, is_displayed=True, is_enabled=True)

        def click(self, **kwargs):
            return None

        def input(self, value, **kwargs):
            return None

        def property(self, name):
            return self.current_value

    def test_native_input_does_not_treat_empty_value_as_success(self):
        element = self.NativeInput(current_value="")
        self.assertFalse(signup_flow._native_type_element(element, "Neo"))

    def test_native_input_accepts_confirmed_value(self):
        element = self.NativeInput(current_value="Neo")
        self.assertTrue(signup_flow._native_type_element(element, "Neo"))

    def test_detects_account_already_registered_notice(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "notices": [
                "Existing account found An account already exists which is associated with this email address. Please login using the login method shown below. Login with email"
            ],
            "body": "Complete your sign up",
            "url": "https://accounts.x.ai/sign-up",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertTrue(notice.startswith("Existing account found"))

    def test_detects_account_already_registered_by_dom_signature(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "signature": {
                "matched": True,
                "name": "existing-account-email-login-card",
                "text": "找到现有账户 已存在与此邮箱地址关联的账户。 使用邮箱登录",
            },
            "notices": [],
            "body": "",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertEqual(notice, "找到现有账户 已存在与此邮箱地址关联的账户。 使用邮箱登录")

    def test_detects_chinese_account_wording_as_text_fallback(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "signature": {"matched": False},
            "notices": ["找到现有账户 已存在与此邮箱地址关联的账户。"],
            "body": "",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertTrue(notice.startswith("找到现有账户"))

    def test_known_text_takes_priority_over_dom_signature(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "signature": {
                "matched": True,
                "name": "existing-account-email-login-card",
                "text": "DOM fallback result",
            },
            "notices": [
                "Existing account found An account already exists which is associated with this email address."
            ],
            "body": "",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        with mock.patch.object(signup_flow, "page", page):
            notice = signup_flow.detect_account_already_registered()
        self.assertTrue(notice.startswith("Existing account found"))

    def test_ignores_generic_existing_account_signin_link(self):
        page = mock.Mock()
        page.run_js.return_value = {
            "notices": [],
            "body": "Already have an account? Sign in",
            "url": "https://accounts.x.ai/sign-up",
        }
        with mock.patch.object(signup_flow, "page", page):
            self.assertEqual(signup_flow.detect_account_already_registered(), "")

    def test_duplicate_account_has_own_failure_type(self):
        exc = signup_flow.AccountAlreadyRegistered("fixture")
        self.assertEqual(engine.classify_failure(exc), engine.FAIL_ALREADY_REGISTERED)

    def test_code_submission_accepts_native_button_label(self):
        logs = []
        page = mock.Mock()
        with mock.patch.dict(
            signup_flow._deps,
            {"get_oai_code": mock.Mock(return_value="123456")},
        ), mock.patch.object(
            signup_flow, "_native_fill_code", return_value="filled-aggregate"
        ), mock.patch.object(
            signup_flow, "_native_click_action", return_value="Continue"
        ), mock.patch.object(
            signup_flow, "sleep_with_cancel"
        ), mock.patch.object(
            signup_flow, "_profile_page_snapshot", return_value={"profile_form": False}
        ), mock.patch.object(
            signup_flow,
            "_wait_profile_page_after_code",
            return_value={"profile_form": True, "url": "https://accounts.x.ai/sign-up"},
        ), mock.patch.object(signup_flow, "page", page):
            result = signup_flow.fill_code_and_submit(
                "fixture@example.com",
                "fixture-token",
                timeout=1,
                log_callback=logs.append,
            )

        self.assertEqual(result, "123456")
        self.assertFalse(page.run_js.called)
        self.assertTrue(any("Continue" in message for message in logs))

    def test_code_submission_detects_profile_page_before_refilling_otp(self):
        logs = []
        with mock.patch.dict(
            signup_flow._deps,
            {"get_oai_code": mock.Mock(return_value="123456")},
        ), mock.patch.object(
            signup_flow,
            "_profile_page_snapshot",
            return_value={"profile_form": True, "url": "https://accounts.x.ai/sign-up"},
        ), mock.patch.object(signup_flow, "_native_fill_code") as fill_code:
            result = signup_flow.fill_code_and_submit(
                "fixture@example.com",
                "fixture-token",
                timeout=1,
                log_callback=logs.append,
            )

        self.assertEqual(result, "123456")
        self.assertFalse(fill_code.called)
        self.assertTrue(any("页面元素识别资料填写页" in message for message in logs))

    def test_hyphenated_numeric_code_is_filled_without_separator(self):
        with mock.patch.dict(
            signup_flow._deps,
            {"get_oai_code": mock.Mock(return_value="134-771")},
        ), mock.patch.object(
            signup_flow,
            "_native_fill_code",
            return_value="filled-aggregate",
        ) as fill_code, mock.patch.object(
            signup_flow,
            "_native_click_action",
            return_value="Continue",
        ), mock.patch.object(
            signup_flow,
            "_profile_page_snapshot",
            return_value={"profile_form": False},
        ), mock.patch.object(
            signup_flow,
            "_wait_profile_page_after_code",
            return_value={"profile_form": True, "url": "https://accounts.x.ai/sign-up"},
        ), mock.patch.object(signup_flow, "page", mock.Mock()):
            result = signup_flow.fill_code_and_submit(
                "fixture@example.com",
                "fixture-token",
                timeout=1,
            )

        self.assertEqual(result, "134-771")
        fill_code.assert_called_once_with("134771")


if __name__ == "__main__":
    unittest.main()
