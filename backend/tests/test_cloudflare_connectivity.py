"""验证 Cloudflare 邮箱渠道健康检查与建号回退的异常保留行为。"""

import unittest
from unittest import mock

from backend.integrations import network_checks
from backend.registration import engine


class CloudflareConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(engine.config)

    def tearDown(self):
        engine.config.clear()
        engine.config.update(self.original_config)

    def _cloudflare_config(self, **overrides):
        config = {
            "cloudflare_api_base": "https://temp-mail.example.com",
            "cloudflare_api_key": "admin-secret",
            "cloudflare_auth_mode": "x-admin-auth",
            "cloudflare_custom_auth": "",
            "cloudflare_path_accounts": "/admin/new_address",
            "cloudflare_path_domains": "/api/domains",
            "cloudflare_path_token": "/api/token",
            "cloudflare_path_messages": "/api/mails",
        }
        config.update(overrides)
        return config

    def test_probe_uses_create_endpoint_not_domains(self):
        # /api/domains 需要邮箱 Bearer JWT，用 admin 头探它必然 401 误报。
        # 健康检查必须改探建号端点（GET 无副作用），否则真实可用也会被判定失败。
        http_get = mock.Mock()
        http_get.return_value.status_code = 404  # POST-only 端点 GET 返回 404 = 鉴权通过
        http_post = mock.Mock()

        name, ok, detail = network_checks.check_email_api(
            "cloudflare",
            self._cloudflare_config(),
            http_get,
            http_post,
        )

        self.assertEqual(name, "邮箱API")
        self.assertTrue(ok, detail)
        self.assertIn("/admin/new_address", detail)
        self.assertNotIn("鉴权失败", detail)
        self.assertEqual(http_get.call_count, 1)
        url = http_get.call_args.args[0]
        self.assertIn("/admin/new_address", url)

    def test_probe_does_not_hit_domains_endpoint(self):
        http_get = mock.Mock()
        http_get.return_value.status_code = 404
        http_post = mock.Mock()

        network_checks.check_email_api(
            "cloudflare",
            self._cloudflare_config(),
            http_get,
            http_post,
        )

        url = http_get.call_args.args[0]
        self.assertNotIn("/api/domains", url)

    def test_probe_reports_401_as_auth_failure(self):
        http_get = mock.Mock()
        http_get.return_value.status_code = 401
        http_post = mock.Mock()

        name, ok, detail = network_checks.check_email_api(
            "cloudflare",
            self._cloudflare_config(),
            http_get,
            http_post,
        )

        self.assertFalse(ok)
        self.assertIn("鉴权失败", detail)

    def test_probe_skips_http_when_auth_is_none(self):
        # auth_mode=none 的直建模式不应发任何 HTTP 探活请求（避免 401 困扰），
        # 只做 TCP 在线探测；用 mock 避免依赖真实网络/假域名。
        http_get = mock.Mock()
        http_post = mock.Mock()

        with mock.patch.object(network_checks, "_tcp_open", return_value=True) as tcp_open:
            name, ok, detail = network_checks.check_email_api(
                "cloudflare",
                self._cloudflare_config(cloudflare_auth_mode="none", cloudflare_api_key=""),
                http_get,
                http_post,
            )

        self.assertEqual(name, "邮箱API")
        self.assertTrue(ok, detail)
        self.assertIn("直建模式", detail)
        tcp_open.assert_called_once_with("temp-mail.example.com", 443)
        http_get.assert_not_called()
        http_post.assert_not_called()

    def test_probe_reports_tcp_failure_when_auth_is_none(self):
        http_get = mock.Mock()
        http_post = mock.Mock()

        with mock.patch.object(network_checks, "_tcp_open", return_value=False):
            name, ok, detail = network_checks.check_email_api(
                "cloudflare",
                self._cloudflare_config(cloudflare_auth_mode="none", cloudflare_api_key=""),
                http_get,
                http_post,
            )

        self.assertEqual(name, "邮箱API")
        self.assertFalse(ok)
        self.assertIn("不可达", detail)
        http_get.assert_not_called()
        http_post.assert_not_called()

    def test_admin_create_sends_x_admin_auth_even_when_mode_none(self):
        # /admin/new_address 在官方文档里要求 x-admin-auth。
        # 即使 UI 把 auth_mode 留成 none，只要配了管理员密码也应带上该头。
        from backend.mailbox import cloudflare_worker as cf

        captured = {}

        class FakeResp:
            status_code = 200
            text = "{}"

            def raise_for_status(self):
                return None

            def json(self):
                return {"address": "demo@example.com", "jwt": "jwt-token"}

        def http_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = dict(kwargs.get("headers") or {})
            captured["json"] = dict(kwargs.get("json") or {})
            return FakeResp()

        address, token = cf.create_temp_address(
            http_post,
            "https://temp-mail.example.com",
            accounts_path="/admin/new_address",
            api_key="admin-secret",
            auth_mode="none",
            name="demo",
        )

        self.assertEqual(address, "demo@example.com")
        self.assertEqual(token, "jwt-token")
        self.assertTrue(captured["url"].endswith("/admin/new_address"))
        self.assertEqual(captured["headers"].get("x-admin-auth"), "admin-secret")
        self.assertEqual(captured["json"].get("name"), "demo")
        self.assertTrue(captured["json"].get("enablePrefix"))

    def test_fallback_preserves_both_errors(self):
        engine.config.update(
            {
                "email_provider": "cloudflare",
                "cloudflare_api_base": "https://temp-mail.example.com",
                "cloudflare_api_key": "admin-secret",
                "cloudflare_auth_mode": "x-admin-auth",
                "cloudflare_custom_auth": "",
                "cloudflare_path_accounts": "/admin/new_address",
                "cloudflare_path_domains": "/api/domains",
                "cloudflare_path_token": "/api/token",
                "cloudflare_path_messages": "/api/mails",
            }
        )

        with (
            mock.patch.object(
                engine,
                "cloudflare_create_temp_address",
                side_effect=RuntimeError("primary 401"),
            ),
            mock.patch.object(
                engine.cloudflare_provider,
                "create_mailbox_fallback",
                side_effect=RuntimeError("fallback 403"),
            ),
        ):
            with self.assertRaises(Exception) as caught:
                engine.get_email_and_token()

        message = str(caught.exception)
        self.assertIn("/admin/new_address", message)
        self.assertIn("primary 401", message)
        self.assertIn("fallback 403", message)

    def test_fallback_returns_successful_result(self):
        engine.config.update(
            {
                "email_provider": "cloudflare",
                "cloudflare_api_base": "https://temp-mail.example.com",
                "cloudflare_api_key": "",
                "cloudflare_auth_mode": "none",
                "cloudflare_custom_auth": "",
                "cloudflare_path_accounts": "/api/new_address",
                "cloudflare_path_domains": "/api/domains",
                "cloudflare_path_token": "/api/token",
                "cloudflare_path_messages": "/api/mails",
            }
        )

        with (
            mock.patch.object(
                engine,
                "cloudflare_create_temp_address",
                side_effect=RuntimeError("primary failed"),
            ),
            mock.patch.object(
                engine.cloudflare_provider,
                "create_mailbox_fallback",
                return_value=("user@example.com", "fallback-token"),
            ),
        ):
            address, token = engine.get_email_and_token()

        self.assertEqual(address, "user@example.com")
        self.assertEqual(token, "fallback-token")


if __name__ == "__main__":
    unittest.main()
