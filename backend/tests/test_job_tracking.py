import unittest
from unittest import mock

from backend.web.jobs import RegistrationJobCoordinator


class RegistrationJobProgressTests(unittest.TestCase):
    def test_tracks_success_failure_stage_and_email(self):
        manager = RegistrationJobCoordinator()
        manager._target_count = 3
        manager._running = True

        manager._append_log("[*] 2. 创建邮箱并提交")
        manager._append_log("[*] 邮箱: first@example.com")
        manager._append_log("[+] 注册成功: first@example.com")
        manager._append_log("[-] 注册失败 [浏览器异常]: failed")

        status = manager.status()
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["success_count"], 1)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["current_email"], "first@example.com")
        self.assertEqual(status["progress_percent"], 66.7)

    def test_browser_start_failure_counts_multiple_tasks_and_caps_target(self):
        manager = RegistrationJobCoordinator()
        manager._target_count = 2
        manager._append_log("[W1] [-] 浏览器启动失败，5 个任务均记为失败: boom")

        status = manager.status()
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["failure_count"], 2)
        self.assertEqual(status["progress_percent"], 100.0)

    def test_request_stop_interrupts_browser_work(self):
        manager = RegistrationJobCoordinator()
        manager._running = True

        class Controller:
            def __init__(self):
                self.stop_requested = False

            def stop(self):
                self.stop_requested = True

            def should_stop(self):
                return self.stop_requested

        manager._stop_controller = Controller()
        with mock.patch("backend.automation.session.interrupt_browser_work") as interrupt:
            status = manager.request_stop()

        interrupt.assert_called_once()
        self.assertTrue(manager._stop_controller.stop_requested)
        self.assertTrue(status["running"])


if __name__ == "__main__":
    unittest.main()
