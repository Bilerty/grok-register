import unittest
from datetime import datetime, timezone

from backend.mailbox import outlook_pool


class FakeResponse:
    def __init__(self, data, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, server):
        self.server = server
        self.cookies = {}
        self.proxies = None

    def post(self, url, **kwargs):
        if str(url).endswith("/api/accounts/batch-update-group"):
            self.server.setdefault("post_calls", []).append(
                {
                    "url": url,
                    "headers": dict(kwargs.get("headers") or {}),
                    "json": kwargs.get("json"),
                }
            )
            return FakeResponse({"success": True, "message": "已将 1 个账号移动到目标分组"})
        self.server["login_calls"] += 1
        self.server["login_payloads"].append(kwargs.get("json"))
        return FakeResponse({"success": True, "launch_url": "/extension-login/once"})

    def get(self, url, **kwargs):
        if "/extension-login/" in url:
            self.cookies["session"] = f"session-{self.server['login_calls']}"
            return FakeResponse({}, headers={"set-cookie": "session=ignored; Path=/"})
        if url.endswith("/api/csrf-token"):
            self.server["csrf_headers"].append(dict(kwargs.get("headers") or {}))
            status_code = (
                self.server["csrf_statuses"].pop(0)
                if self.server["csrf_statuses"]
                else 200
            )
            if status_code != 200:
                return FakeResponse({"success": False}, status_code=status_code)
            return FakeResponse(
                {"csrf_token": "csrf-value", "csrf_disabled": False},
                headers={"set-cookie": "csrf_session=bound; Path=/"},
            )
        if url.endswith("/api/groups"):
            self.server.setdefault("group_calls", []).append(
                {"url": url, "headers": dict(kwargs.get("headers") or {})}
            )
            status_code = (
                self.server["group_statuses"].pop(0)
                if self.server.get("group_statuses")
                else 200
            )
            if status_code != 200:
                return FakeResponse({"success": False, "error": "请先登录"}, status_code=status_code)
            return FakeResponse(
                self.server.get("groups_payload")
                or {
                    "success": True,
                    "groups": [
                        {"id": 1, "name": "默认分组", "account_count": 12, "is_system": 0},
                        {"id": 14, "name": "验证码超时", "account_count": 0, "is_system": 0},
                        {"id": 2, "name": "临时邮箱", "account_count": 3, "is_system": 1},
                    ],
                }
            )
        raise AssertionError(url)

    def put(self, url, **kwargs):
        self.server["put_calls"].append(
            {
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "json": kwargs.get("json"),
            }
        )
        return FakeResponse({"success": True, "message": "状态更新成功"})


class OutlookEmailDisableTests(unittest.TestCase):
    def setUp(self):
        outlook_pool.reset_runtime_state()
        self.server = {
            "login_calls": 0,
            "login_payloads": [],
            "csrf_headers": [],
            "csrf_statuses": [],
            "put_calls": [],
            "post_calls": [],
        }

    def session_factory(self):
        return FakeSession(self.server)

    @staticmethod
    def http_get(url, **kwargs):
        if url.endswith("/api/external/accounts"):
            return FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 367,
                            "email": "fixture@outlook.com",
                            "status": "active",
                            "group_id": 1,
                        }
                    ],
                }
            )
        raise AssertionError(url)

    def test_password_login_csrf_and_put_inactive(self):
        email, _ = outlook_pool.acquire_email(
            self.http_get,
            self.session_factory,
            "http://mail-pool.test",
            api_key="api-key",
            source="accounts",
            pick_mode="sequential",
        )
        result = outlook_pool.disable_account(
            self.http_get,
            self.session_factory,
            "http://mail-pool.test",
            email,
            api_key="api-key",
            web_password="web-password",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], 367)
        self.assertEqual(self.server["login_calls"], 1)
        self.assertEqual(
            self.server["login_payloads"],
            [{"password": "web-password", "next": "/"}],
        )
        self.assertEqual(len(self.server["put_calls"]), 1)
        self.assertEqual(
            self.server["csrf_headers"],
            [{"Accept": "application/json"}],
        )
        request = self.server["put_calls"][0]
        self.assertTrue(request["url"].endswith("/api/accounts/367"))
        self.assertEqual(request["json"], {"status": "inactive"})
        self.assertEqual(request["headers"]["X-CSRFToken"], "csrf-value")
        self.assertNotIn("Cookie", request["headers"])

    def test_internal_docker_hostname_keeps_web_session(self):
        result = outlook_pool.disable_account(
            self.http_get,
            self.session_factory,
            "http://outlook-email:5000",
            "fixture@outlook.com",
            api_key="api-key",
            web_password="web-password",
        )

        self.assertTrue(result["success"])
        self.assertEqual(self.server["login_calls"], 1)
        self.assertNotIn("Cookie", self.server["csrf_headers"][0])
        self.assertNotIn("Cookie", self.server["put_calls"][0]["headers"])

    def test_seeded_cookie_uses_api_hostname_scope(self):
        calls = []

        class CookieJar:
            def set(self, name, value, **kwargs):
                calls.append((name, value, kwargs))

        class Session:
            cookies = CookieJar()

        self.assertTrue(
            outlook_pool.seed_session_cookie(
                Session(),
                "session=session-1",
                "http://outlook-email:5000",
            )
        )
        self.assertEqual(
            calls,
            [("session", "session-1", {"domain": ".outlook-email", "path": "/"})],
        )

    def test_already_inactive_is_idempotent_without_login(self):
        def inactive_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {"id": 8, "email": "inactive@outlook.com", "status": "inactive"}
                    ],
                }
            )

        result = outlook_pool.disable_account(
            inactive_get,
            self.session_factory,
            "http://mail-pool.test",
            "inactive@outlook.com",
            api_key="api-key",
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["already_inactive"])
        self.assertEqual(self.server["login_calls"], 0)
        self.assertEqual(self.server["put_calls"], [])

    def test_expired_session_refreshes_password_login_once(self):
        self.server["csrf_statuses"] = [401, 200]
        result = outlook_pool.disable_account(
            self.http_get,
            self.session_factory,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            web_password="web-password",
        )
        self.assertTrue(result["success"])
        self.assertEqual(self.server["login_calls"], 2)
        self.assertEqual(len(self.server["put_calls"]), 1)

    def test_http_error_includes_request_and_response_details(self):
        class ErrorResponse(FakeResponse):
            text = '{"success":false,"error":"invalid status"}'

            def raise_for_status(self):
                return None

        def error_session_factory():
            session = FakeSession(self.server)

            def put(url, **kwargs):
                self.server["put_calls"].append(
                    {"url": url, "headers": dict(kwargs.get("headers") or {}), "json": kwargs.get("json")}
                )
                return ErrorResponse({"success": False, "error": "invalid status"}, status_code=400)

            session.put = put
            return session

        with self.assertRaisesRegex(
            Exception,
            r"停用请求失败: HTTP 400; url=.*/api/accounts/367; request_body=\{'status': 'inactive'\}; response_body=",
        ):
            outlook_pool.disable_account(
                self.http_get,
                error_session_factory,
                "http://mail-pool.test",
                "fixture@outlook.com",
                api_key="api-key",
                web_password="web-password",
            )

    def test_rotated_csrf_session_cookie_is_sent_by_session_jar(self):
        class RotatingSession(FakeSession):
            def get(self, url, **kwargs):
                if url.endswith("/api/csrf-token"):
                    self.cookies["session"] = "rotated-session"
                    return FakeResponse({"csrf_token": "csrf-value", "csrf_disabled": False})
                return super().get(url, **kwargs)

            def put(self, url, **kwargs):
                headers = dict(kwargs.get("headers") or {})
                self.server["put_calls"].append({"url": url, "headers": headers, "json": kwargs.get("json")})
                assert headers.get("X-CSRFToken") == "csrf-value"
                assert "Cookie" not in headers
                assert self.cookies.get("session") == "rotated-session"
                return FakeResponse({"success": True})

        def factory():
            return RotatingSession(self.server)

        result = outlook_pool.disable_account(
            self.http_get,
            factory,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            session_cookie="session=initial",
        )
        self.assertTrue(result["success"])


class OutlookEmailMoveGroupTests(unittest.TestCase):
    def setUp(self):
        outlook_pool.reset_runtime_state()
        self.server = {
            "login_calls": 0,
            "login_payloads": [],
            "csrf_headers": [],
            "csrf_statuses": [],
            "put_calls": [],
            "post_calls": [],
        }

    def session_factory(self):
        return FakeSession(self.server)

    @staticmethod
    def http_get(url, **kwargs):
        if url.endswith("/api/external/accounts"):
            return FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 367,
                            "email": "fixture@outlook.com",
                            "status": "active",
                            "group_id": 1,
                        }
                    ],
                }
            )
        raise AssertionError(url)

    def test_move_posts_batch_update_group_without_changing_status(self):
        result = outlook_pool.move_account_to_group(
            self.http_get,
            self.session_factory,
            "http://mail-pool.test",
            "fixture@outlook.com",
            "14",
            api_key="api-key",
            group_id="1",
            web_password="web-password",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], 367)
        self.assertEqual(result["group_id"], 14)
        self.assertFalse(result["already_moved"])
        self.assertEqual(self.server["put_calls"], [])
        self.assertEqual(len(self.server["post_calls"]), 1)
        request = self.server["post_calls"][0]
        self.assertTrue(request["url"].endswith("/api/accounts/batch-update-group"))
        self.assertEqual(request["json"], {"account_ids": [367], "group_id": 14})
        self.assertEqual(request["headers"]["X-CSRFToken"], "csrf-value")

    def test_already_in_target_group_is_idempotent_without_login(self):
        def grouped_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "accounts": [
                        {
                            "id": 9,
                            "email": "moved@outlook.com",
                            "status": "active",
                            "group_id": 14,
                        }
                    ],
                }
            )

        result = outlook_pool.move_account_to_group(
            grouped_get,
            self.session_factory,
            "http://mail-pool.test",
            "moved@outlook.com",
            14,
            api_key="api-key",
            group_id="14",
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["already_moved"])
        self.assertEqual(result["group_id"], 14)
        self.assertEqual(self.server["login_calls"], 0)
        self.assertEqual(self.server["post_calls"], [])
        self.assertEqual(self.server["put_calls"], [])

    def test_invalid_target_group_id_is_rejected(self):
        with self.assertRaisesRegex(Exception, "目标分组 ID 无效"):
            outlook_pool.move_account_to_group(
                self.http_get,
                self.session_factory,
                "http://mail-pool.test",
                "fixture@outlook.com",
                "abc",
                api_key="api-key",
            )


class OutlookEmailGroupListTests(unittest.TestCase):
    def setUp(self):
        outlook_pool.reset_runtime_state()
        self.server = {
            "login_calls": 0,
            "login_payloads": [],
            "csrf_headers": [],
            "csrf_statuses": [],
            "put_calls": [],
            "post_calls": [],
            "group_calls": [],
            "group_statuses": [],
        }

    def session_factory(self):
        return FakeSession(self.server)

    def test_list_groups_uses_web_session_and_keeps_names(self):
        groups = outlook_pool.list_groups(
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should use web session")),
            self.session_factory,
            "http://mail-pool.test",
            web_password="web-password",
        )
        self.assertEqual(
            [(item["id"], item["name"], item["is_system"]) for item in groups],
            [(1, "默认分组", False), (2, "临时邮箱", True), (14, "验证码超时", False)],
        )
        self.assertEqual(self.server["login_calls"], 1)
        self.assertEqual(len(self.server["group_calls"]), 1)
        self.assertTrue(self.server["group_calls"][0]["url"].endswith("/api/groups"))

    def test_list_groups_falls_back_to_accounts_when_no_web_login(self):
        def http_get(url, **kwargs):
            if url.endswith("/api/external/accounts"):
                return FakeResponse(
                    {
                        "success": True,
                        "accounts": [
                            {"id": 1, "email": "a@outlook.com", "group_id": 1, "group_name": "默认分组"},
                            {"id": 2, "email": "b@outlook.com", "group_id": 1, "group_name": "默认分组"},
                            {"id": 3, "email": "c@outlook.com", "group_id": 14, "group_name": "验证码超时"},
                        ],
                    }
                )
            raise AssertionError(url)

        groups = outlook_pool.list_groups(
            http_get,
            self.session_factory,
            "http://mail-pool.test",
            api_key="api-key",
        )
        self.assertEqual(self.server["login_calls"], 0)
        self.assertEqual(
            [(item["id"], item["name"], item["account_count"]) for item in groups],
            [(1, "默认分组", 2), (14, "验证码超时", 1)],
        )


class OutlookEmailCodeTimeTests(unittest.TestCase):
    def test_message_received_at_supports_api_timestamp_formats(self):
        self.assertEqual(
            outlook_pool.message_received_at({"timestamp": 1_700_000_000_000}),
            1_700_000_000,
        )
        self.assertEqual(
            outlook_pool.message_received_at({"date": "2026-08-04T12:00:00Z"}),
            datetime(2026, 8, 4, 12, tzinfo=timezone.utc).timestamp(),
        )
        self.assertIsNone(outlook_pool.message_received_at({"date": "unknown"}))

    def test_wait_for_code_ignores_messages_before_submission(self):
        submitted_at = 1_700_000_000.5
        requested = []

        def http_get(url, **kwargs):
            requested.append((url, kwargs))
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "old",
                            "subject": "OLD-111 xAI",
                            "date": submitted_at - 1,
                            "body_preview": "OLD-111",
                        },
                        {
                            "id": "boundary",
                            "subject": "BND-333 xAI",
                            "date": submitted_at,
                            "body_preview": "BND-333",
                        },
                        {
                            "id": "missing-time",
                            "subject": "MIS-444 xAI",
                            "body_preview": "MIS-444",
                        },
                        {
                            "id": "new",
                            "subject": "NEW-222 xAI",
                            "date": submitted_at + 1,
                            "body_preview": "NEW-222",
                        },
                    ],
                }
            )

        code = outlook_pool.wait_for_code(
            http_get,
            lambda: None,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            source="accounts",
            timeout=1,
            poll_interval=0,
            min_received_at=submitted_at,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )

        self.assertEqual(code, "NEW-222")
        self.assertEqual(requested[0][1]["params"]["email"], "fixture@outlook.com")

    def test_wait_for_code_reads_numeric_hyphenated_subject(self):
        submitted_at = 1_786_770_721.584

        def http_get(url, **kwargs):
            return FakeResponse(
                {
                    "success": True,
                    "emails": [
                        {
                            "id": "new-code",
                            "subject": "SpaceXAI confirmation code: 180-699",
                            "date": submitted_at + 1,
                            "from": "noreply@x.ai",
                        }
                    ],
                }
            )

        code = outlook_pool.wait_for_code(
            http_get,
            lambda: None,
            "http://mail-pool.test",
            "fixture@outlook.com",
            api_key="api-key",
            source="accounts",
            timeout=1,
            poll_interval=0,
            min_received_at=submitted_at,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )

        self.assertEqual(code, "180-699")


if __name__ == "__main__":
    unittest.main()
