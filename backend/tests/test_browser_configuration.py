import unittest
from unittest import mock

from backend.automation import session as browser_session
from backend.registration import engine as gr


class BrowserHeadlessConfigTests(unittest.TestCase):
    def tearDown(self):
        browser_session.stop_browser(force=True)
        browser_session.allow_browser_launches()
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "camoufox",
        )

    def test_camoufox_remains_default_browser_engine(self):
        browser_session.configure(get_engine=None)
        self.assertEqual(browser_session.selected_browser_engine(), "camoufox")

    def test_invalid_browser_engine_falls_back_to_camoufox(self):
        browser_session.configure(get_engine=lambda: "unknown")
        self.assertEqual(browser_session.selected_browser_engine(), "camoufox")

    def test_browser_options_follow_headless_setting(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: True,
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertIs(options["headless"], True)

        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertIs(options["headless"], False)

    def test_container_force_headed_overrides_config(self):
        with mock.patch.dict(gr.os.environ, {"GROK_FORCE_HEADED": "1"}, clear=False):
            with mock.patch.dict(gr.config, {"browser_headless": True}, clear=False):
                self.assertFalse(gr.is_browser_headless())

    def test_browser_options_force_configured_locale(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "zh-CN",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(options["locale"], "zh-CN")

    def test_invalid_browser_locale_falls_back_to_english(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "fr-FR",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(options["locale"], "en-US")

    def test_cloakbrowser_options_share_proxy_locale_and_headless_settings(self):
        browser_session.configure(
            get_proxies=lambda: {"https": "http://user:pass@proxy.example.com:8080"},
            is_debug=lambda: False,
            is_headless=lambda: True,
            get_locale=lambda: "zh-CN",
            get_engine=lambda: "cloakbrowser",
        )

        options = browser_session.create_browser_options(unique_profile=False)

        self.assertIs(options["headless"], True)
        self.assertIs(options["humanize"], True)
        self.assertIs(options["geoip"], True)
        self.assertEqual(options["locale"], "zh-CN")
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:8080",
                "username": "user",
                "password": "pass",
            },
        )

    def test_start_browser_dispatches_to_cloakbrowser_backend(self):
        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]
                self.closed = False

            def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

            def close(self):
                self.closed = True

        context = FakeContext()
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "cloakbrowser",
        )
        with mock.patch.object(
            browser_session,
            "_launch_cloakbrowser_context",
            return_value=(context, None),
        ) as launch:
            browser, page = browser_session.start_browser()

        self.assertEqual(browser.engine_name, "cloakbrowser")
        self.assertIs(page.raw_page, context.pages[0])
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
