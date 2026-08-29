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
            is_low_traffic=lambda: False,
            get_traffic_savings_level=lambda: "standard",
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

    def test_low_traffic_mode_excludes_default_camoufox_addons(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            is_low_traffic=lambda: True,
        )
        sentinel = [object()]
        with mock.patch.object(
            browser_session,
            "_ensure_default_addons_or_exclude",
            return_value=sentinel,
        ) as exclude:
            options = browser_session.create_camoufox_options(unique_profile=False)

        exclude.assert_called_once_with(disable_defaults=True)
        self.assertIs(options["exclude_addons"], sentinel)
        self.assertEqual(options["timeout"], browser_session._BROWSER_LAUNCH_TIMEOUT_MS)

    def test_low_traffic_request_rules_preserve_registration_and_turnstile(self):
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_block(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_block(
                "https://cdn.grok.com/assets/hero.webp", "image"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_block(
                "https://media.x.ai/video.mp4", "media"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_block(
                "https://challenges.cloudflare.com/turnstile/v0/api.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_block(
                "https://accounts.x.ai/api/register", "fetch"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/sign-up", "document"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/cdn-cgi/challenge-platform/main.js", "script"
            )
        )

    def test_more_savings_caches_accounts_hashed_static_resources(self):
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "standard",
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )

        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/sign-up", "document"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/cdn-cgi/challenge-platform/main.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/api/register", "fetch"
            )
        )

    def test_accounts_resource_diagnostics_logs_real_response_size_in_debug(self):
        browser_session.configure(is_debug=lambda: True)
        context = mock.Mock()
        logs = []
        browser_session._install_accounts_resource_diagnostics(context, logs.append)
        callback = context.on.call_args.args[1]
        response = mock.Mock(
            status=200, headers={"content-length": "999"}
        )
        request = mock.Mock(
            url="https://accounts.x.ai/assets/app.js?build=secret",
            resource_type="script",
            response=mock.Mock(return_value=response),
            sizes=mock.Mock(return_value={"responseBodySize": 5}),
        )

        callback(request)

        self.assertEqual(len(logs), 1)
        self.assertIn("type=script status=200 bytes=5", logs[0])
        self.assertIn("url=https://accounts.x.ai/assets/app.js", logs[0])
        self.assertNotIn("build=secret", logs[0])

    def test_accounts_resource_diagnostics_ignores_other_hosts(self):
        browser_session.configure(is_debug=lambda: True)
        context = mock.Mock()
        logs = []
        browser_session._install_accounts_resource_diagnostics(context, logs.append)
        callback = context.on.call_args.args[1]

        callback(
            mock.Mock(
                url="https://cdn.grok.com/assets/app.js",
                status=200,
                headers={},
                request=mock.Mock(resource_type="script"),
            )
        )

        self.assertEqual(logs, [])

    def test_accounts_resource_diagnostics_is_disabled_outside_debug(self):
        browser_session.configure(is_debug=lambda: False)
        context = mock.Mock()

        browser_session._install_accounts_resource_diagnostics(context, mock.Mock())

        context.on.assert_not_called()

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
