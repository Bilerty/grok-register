import unittest
from unittest import mock

from backend.automation import session as browser_session


class CamoufoxProcessMatchTests(unittest.TestCase):
    def tearDown(self):
        browser_session.allow_browser_launches()

    def test_matches_camoufox_executables_and_managed_profiles(self):
        self.assertTrue(browser_session._is_camoufox_process("/cache/camoufox/camoufox-bin", ""))
        self.assertTrue(
            browser_session._is_camoufox_process(
                "/usr/lib/firefox/firefox",
                "firefox -profile /tmp/grok-register-camoufox/123-profile",
            )
        )

    def test_does_not_match_regular_firefox(self):
        self.assertFalse(
            browser_session._is_camoufox_process(
                "/usr/lib/firefox/firefox",
                "firefox https://example.com",
            )
        )

    def test_matches_only_managed_cloakbrowser_chromium(self):
        self.assertTrue(
            browser_session._is_cloakbrowser_process(
                "/app/data/cloakbrowser-cache/chromium/chrome",
                "chrome --user-data-dir=/tmp/grok-register-cloakbrowser/123-profile",
            )
        )
        self.assertFalse(
            browser_session._is_cloakbrowser_process(
                "/usr/bin/google-chrome",
                "google-chrome https://example.com",
            )
        )

    def test_emergency_block_prevents_browser_restart(self):
        browser_session.block_browser_launches()
        with self.assertRaisesRegex(RuntimeError, "紧急终止"):
            browser_session.start_browser()

    def test_kill_all_targets_camoufox_tree_only(self):
        processes = {
            101: (1, "/cache/camoufox/camoufox", "camoufox"),
            102: (101, "/usr/lib/helper", "content process"),
            201: (1, "/usr/lib/firefox/firefox", "firefox https://example.com"),
        }
        killed = []
        with (
            mock.patch.object(browser_session, "_linux_processes", return_value=processes),
            mock.patch.object(browser_session, "_cleanup_all_managed_profiles", return_value=2),
            mock.patch.object(browser_session.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            result = browser_session.kill_all_camoufox_processes()

        self.assertEqual(result, {"killed": 2, "profiles_cleaned": 2})
        self.assertEqual({pid for pid, _ in killed}, {101, 102})
        self.assertNotIn(201, {pid for pid, _ in killed})

    def test_kill_all_browser_backends_keeps_regular_browsers(self):
        processes = {
            101: (1, "/cache/camoufox/camoufox", "camoufox"),
            102: (101, "/usr/lib/helper", "content process"),
            301: (
                1,
                "/app/data/cloakbrowser-cache/chromium/chrome",
                "chrome --user-data-dir=/tmp/grok-register-cloakbrowser/301-profile",
            ),
            302: (301, "/app/data/cloakbrowser-cache/chromium/chrome", "--type=renderer"),
            401: (1, "/usr/bin/google-chrome", "google-chrome https://example.com"),
        }
        killed = []
        with (
            mock.patch.object(browser_session, "_linux_processes", return_value=processes),
            mock.patch.object(browser_session, "_cleanup_all_managed_profiles", return_value=3),
            mock.patch.object(browser_session.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            result = browser_session.kill_all_browser_processes()

        self.assertEqual(result, {"killed": 4, "profiles_cleaned": 3})
        self.assertEqual({pid for pid, _ in killed}, {101, 102, 301, 302})
        self.assertNotIn(401, {pid for pid, _ in killed})


if __name__ == "__main__":
    unittest.main()
