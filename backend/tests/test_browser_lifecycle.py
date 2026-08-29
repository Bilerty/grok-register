import os
import threading
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


class BrowserLaunchInterruptTests(unittest.TestCase):
    def tearDown(self):
        browser_session.allow_browser_launches()

    def test_matches_local_playwright_run_driver(self):
        project = str(browser_session.Path(__file__).resolve().parents[2]).replace("\\", "/").lower()
        command = f"{project}/.venv/lib/python3.12/site-packages/playwright/driver/node cli.js run-driver"
        self.assertTrue(
            browser_session._is_local_playwright_driver(
                501, os.getpid(), "/usr/bin/node", command
            )
        )
        self.assertFalse(
            browser_session._is_local_playwright_driver(
                601,
                1,
                "/usr/bin/node",
                "/other/app/.venv/lib/python3.12/site-packages/playwright/driver/node cli.js run-driver",
            )
        )

    def test_kill_all_also_stops_local_playwright_drivers(self):
        project = str(browser_session.Path(__file__).resolve().parents[2])
        processes = {
            101: (1, "/cache/camoufox/camoufox", "camoufox"),
            102: (101, "/usr/lib/helper", "content process"),
            501: (
                os.getpid(),
                "/usr/bin/node",
                f"{project}/.venv/lib/python3.12/site-packages/playwright/driver/node cli.js run-driver",
            ),
            502: (501, "/usr/bin/node", "playwright helper"),
            601: (
                1,
                "/usr/bin/node",
                "/other/app/.venv/lib/python3.12/site-packages/playwright/driver/node cli.js run-driver",
            ),
        }
        killed = []
        with (
            mock.patch.object(browser_session, "_linux_processes", return_value=processes),
            mock.patch.object(browser_session, "_cleanup_all_managed_profiles", return_value=1),
            mock.patch.object(browser_session.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            result = browser_session.kill_all_browser_processes()

        self.assertEqual(result["killed"], 4)
        self.assertEqual(result["profiles_cleaned"], 1)
        self.assertEqual({pid for pid, _ in killed}, {101, 102, 501, 502})
        self.assertNotIn(601, {pid for pid, _ in killed})

    def test_start_browser_checks_cancel_before_launch(self):
        with mock.patch.object(browser_session, "_launch_camoufox_context") as launch:
            with self.assertRaisesRegex(RuntimeError, "紧急终止"):
                browser_session.start_browser(cancel_callback=lambda: True)
        launch.assert_not_called()

    def test_start_browser_does_not_retry_after_cancel(self):
        calls = []

        def fake_launch(opts):
            calls.append(opts)
            raise RuntimeError("launch hung")

        with (
            mock.patch.object(browser_session, "create_browser_options", return_value={"headless": True, "locale": "en-US"}),
            mock.patch.object(browser_session, "_launch_camoufox_context", side_effect=fake_launch),
            mock.patch.object(browser_session, "_close_unwrapped_context"),
            mock.patch.object(browser_session, "_cleanup_profile_dir"),
            mock.patch.object(browser_session.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "紧急终止"):
                browser_session.start_browser(cancel_callback=lambda: len(calls) >= 1)
        self.assertEqual(len(calls), 1)

    def test_waiting_worker_can_leave_launch_gate_when_cancelled(self):
        self.assertTrue(browser_session._browser_launch_gate.acquire(blocking=False))
        cancelled = {"value": False}
        errors = []

        def run():
            try:
                browser_session.start_browser(cancel_callback=lambda: cancelled["value"])
            except Exception as exc:
                errors.append(exc)

        try:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(timeout=0.6)
            self.assertTrue(thread.is_alive())
            cancelled["value"] = True
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertIn("紧急终止", str(errors[0]))
        finally:
            try:
                browser_session._browser_launch_gate.release()
            except RuntimeError:
                pass

    def test_restart_browser_forwards_cancel_callback(self):
        cancel = lambda: False
        with (
            mock.patch.object(browser_session, "stop_browser") as stop,
            mock.patch.object(browser_session, "start_browser", return_value=("browser", "page")) as start,
        ):
            result = browser_session.restart_browser(log_callback="log", cancel_callback=cancel)
        stop.assert_called_once_with(force=True)
        start.assert_called_once_with(log_callback="log", cancel_callback=cancel)
        self.assertEqual(result, ("browser", "page"))



if __name__ == "__main__":
    unittest.main()
