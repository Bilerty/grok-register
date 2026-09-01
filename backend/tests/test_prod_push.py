# -*- coding: utf-8 -*-
"""prod_push 单元测试：回调判定口径 / SID 提取 / 出口保护匹配 / outbox 队列。"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.integrations import prod_push as pp
from backend.integrations import proxy_pool
from backend.integrations.grok2api_client import Grok2APIClient
from backend.registration.store import RegistrationRepository


class PushableTests(unittest.TestCase):
    def test_passed_and_not_degraded_is_pushable(self):
        ok, reason = pp.is_pushable_grokiq_result(
            {"degraded": False, "isolated": False, "probe_outcome": "passed"}
        )
        self.assertTrue(ok, reason)

    def test_degraded_not_pushable(self):
        ok, reason = pp.is_pushable_grokiq_result(
            {"degraded": True, "probe_outcome": "passed"}
        )
        self.assertFalse(ok)
        self.assertIn("降智", reason)

    def test_isolated_not_pushable(self):
        ok, reason = pp.is_pushable_grokiq_result(
            {"degraded": False, "isolated": True, "probe_outcome": "passed"}
        )
        self.assertFalse(ok)
        self.assertIn("隔离", reason)

    def test_probe_not_passed_not_pushable(self):
        for outcome in ("", "failed", "partial", "PASSED "):
            ok, reason = pp.is_pushable_grokiq_result(
                {"degraded": False, "probe_outcome": outcome}
            )
            if outcome == "PASSED ":
                self.assertTrue(ok)  # 大小写与空白容错
            else:
                self.assertFalse(ok)
                self.assertIn("probe_outcome", reason)

    def test_real_payload_shape_is_pushable(self):
        payload = {
            "event_type": "grokiq.notify",
            "degraded": False,
            "isolated": False,
            "monitor_status": "healthy",
            "probe_outcome": "passed",
        }
        ok, _ = pp.is_pushable_grokiq_result(payload)
        self.assertTrue(ok)


class SidTests(unittest.TestCase):
    def test_extract_sid(self):
        name = "1024proxy-AT-AS8412-mqHURCt4"
        self.assertEqual(pp.extract_sid(name), "mqHURCt4")

    def test_extract_sid_trims_spaces(self):
        self.assertEqual(pp.extract_sid(" vendor-DE-AS1 - abc123 "), "abc123")

    def test_extract_sid_without_separator_returns_name(self):
        self.assertEqual(pp.extract_sid("solo"), "solo")
        self.assertEqual(pp.extract_sid(""), "")


class CasefoldAuthFileTests(unittest.TestCase):
    def test_find_auth_files_matches_case_insensitively(self):
        from backend.integrations import auth_exchange

        with tempfile.TemporaryDirectory() as tmp:
            # 文件按原始大小写命名（注册 email 为大写）
            with open(os.path.join(tmp, "g2a-OE8SCP0R22@outlook.com.json"), "w") as f:
                f.write("{}")
            # 查找用小写 email（webhook 链路小写化后的形态）
            found = auth_exchange.find_grok2api_auth_files(
                "oe8scp0r22@outlook.com", tmp
            )
            self.assertIn("grok_build", found)
            self.assertEqual(
                found["grok_build"].name, "g2a-OE8SCP0R22@outlook.com.json"
            )


class SelectedDomainsTests(unittest.TestCase):
    def test_intersection_with_staging_targets(self):
        config = {
            "grok2api_auto_import": True,
            "grok2api_auto_import_build": True,
            "grok2api_auto_import_web": False,
            "grok2api_auto_import_console": True,
            "prod_push_build": True,
            "prod_push_web": True,
            "prod_push_console": False,
        }
        self.assertEqual(pp.selected_domains(config), ("grok_build",))

    def test_master_import_off_yields_empty(self):
        config = {
            "grok2api_auto_import": False,
            "grok2api_auto_import_build": True,
            "prod_push_build": True,
        }
        self.assertEqual(pp.selected_domains(config), ())


class FakeClient:
    """按方法名 stub 的 grok2api 客户端。"""

    def __init__(self, accounts=None, nodes=None, fail_import=False):
        self.accounts = accounts or {}
        self.nodes = nodes or []
        self.fail_import = fail_import
        self.imported = []
        self.assigned = []
        self.disabled = []

    def import_auth_file(self, path, format_name="grok_build"):
        if self.fail_import:
            raise RuntimeError(f"import fail {format_name}")
        self.imported.append((str(path), format_name))
        return {"created": 1}

    def search_account(self, provider, email):
        entry = self.accounts.get((provider, email.lower()))
        return dict(entry) if entry else None

    def list_egress_nodes(self, scope):
        # 与真实客户端一致：把 API 的 proxyConfigured 映射为 proxy_configured
        return [
            {
                "id": str(n["id"]),
                "name": n["name"],
                "enabled": bool(n.get("enabled")),
                "proxy_configured": bool(n.get("proxyConfigured")),
            }
            for n in self.nodes
            if n["scope"] == scope
        ]

    def assign_account(self, provider, node_id, account_id, mode="manual"):
        self.assigned.append((provider, str(node_id), str(account_id), mode))

    def disable_accounts(self, provider, account_ids):
        self.disabled.append((provider, [str(i) for i in account_ids]))
        return len(account_ids)


def make_fake_grok2api_class(staging, prod):
    """构造一个可替换 pp.Grok2APIClient 的假类：按 URL 中的 staging/prod 选实现。"""

    class FakeG2AClient:
        PROVIDER_NODE_SCOPES = Grok2APIClient.PROVIDER_NODE_SCOPES
        AUTO_IMPORT_DEFAULTS = Grok2APIClient.AUTO_IMPORT_DEFAULTS

        def __init__(self, url, username, password, **kwargs):
            self.impl = type(self).impls["staging" if "staging" in str(url) else "prod"]

        @staticmethod
        def is_configured(config):
            return Grok2APIClient.is_configured(config)

        @classmethod
        def from_config(cls, config, **kwargs):
            return cls(
                str(config.get("grok2api_remote_url") or ""),
                str(config.get("grok2api_remote_username") or ""),
                str(config.get("grok2api_remote_password") or ""),
            )

        def __getattr__(self, name):
            return getattr(self.impl, name)

    FakeG2AClient.impls = {"staging": staging, "prod": prod}
    return FakeG2AClient


class ExecuteProdPushTests(unittest.TestCase):
    def _repository(self, tmp, record_extra=None):
        repo = RegistrationRepository(os.path.join(tmp, "results.sqlite3"))
        record_id = repo.add_result(
            {
                "batch_id": "b",
                "email": "user1@example.com",
                "status": "success",
                "success": True,
                "account_file": "x.txt",
                "extra": record_extra or {},
            }
        )
        return repo, record_id

    def _config(self, **overrides):
        config = {
            "prod_push_enabled": True,
            "prod_grok2api_remote_url": "http://prod:8000",
            "prod_grok2api_remote_username": "admin",
            "prod_grok2api_remote_password": "pw",
            "grok2api_remote_url": "http://staging:8000",
            "grok2api_remote_username": "admin",
            "grok2api_remote_password": "pw",
            "grok2api_auto_import": True,
            "grok2api_auto_import_build": True,
            "grok2api_auto_import_web": True,
            "grok2api_auto_import_console": False,
            "prod_push_build": True,
            "prod_push_web": True,
            "prod_push_console": False,
            "prod_push_egress_guard": True,
            "grok2api_auth_dir": "",
        }
        config.update(overrides)
        return config

    @mock.patch.object(Grok2APIClient, "login", lambda self: "token")
    def test_push_with_sid_match_and_staging_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = os.path.join(tmp, "auth")
            os.makedirs(auth_dir)
            for prefix in ("g2a-", "grok-web-"):
                with open(os.path.join(auth_dir, f"{prefix}user1@example.com.json"), "w") as f:
                    f.write("{}")
            repo, record_id = self._repository(tmp)
            email = "user1@example.com"

            staging = FakeClient(
                accounts={
                    ("grok_build", email): {"id": "11", "egress_node_id": "77"},
                    ("grok_web", email): {"id": "12", "egress_node_id": "78"},
                },
                nodes=[
                    {"id": "77", "name": "1024proxy-AT-AS8412-sidAAA", "scope": "grok_build", "enabled": True, "proxyConfigured": True},
                    {"id": "78", "name": "vendor-AT-AS8412-sidCCC", "scope": "grok_web", "enabled": True, "proxyConfigured": True},
                ],
            )
            prod = FakeClient(
                accounts={
                    ("grok_build", email): {"id": "101"},
                    ("grok_web", email): {"id": "102"},
                },
                nodes=[
                    {"id": "1", "name": "1024proxy-AT-AS8412-sidAAA", "scope": "grok_build", "enabled": True, "proxyConfigured": True},
                    {"id": "2", "name": "1024proxy-DE-AS2-sidBBB", "scope": "grok_build", "enabled": True, "proxyConfigured": True},
                    {"id": "3", "name": "vendor-DE-AS5-sidZZZ", "scope": "grok_web", "enabled": True, "proxyConfigured": True},
                    {"id": "4", "name": "vendor-DE-AS9-sidDDD", "scope": "grok_web", "enabled": False, "proxyConfigured": True},
                ],
            )
            with mock.patch.object(pp, "Grok2APIClient", make_fake_grok2api_class(staging, prod)):
                result = pp.execute_prod_push(
                    repo, record_id, email, self._config(grok2api_auth_dir=auth_dir)
                )

            self.assertEqual(result["status"], "pushed", result)
            build = result["domains"]["grok_build"]
            self.assertTrue(build["matched"])
            self.assertEqual(build["sid"], "sidAAA")
            self.assertEqual(build["node_name"], "1024proxy-AT-AS8412-sidAAA")
            web = result["domains"]["grok_web"]
            self.assertFalse(web["matched"])  # staging sidAAA 在 web scope 无同源节点 → 随机
            self.assertEqual(prod.assigned[0][0], "grok_build")
            self.assertEqual(prod.assigned[0][2], "101")
            self.assertEqual(staging.disabled, [("grok_build", ["11"]), ("grok_web", ["12"])])

    @mock.patch.object(Grok2APIClient, "login", lambda self: "token")
    def test_guard_disabled_and_missing_file_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, record_id = self._repository(tmp)
            email = "user1@example.com"
            auth_dir = os.path.join(tmp, "auth")
            os.makedirs(auth_dir)
            with open(os.path.join(auth_dir, "g2a-user1@example.com.json"), "w") as f:
                f.write("{}")
            staging = FakeClient(accounts={("grok_build", email): {"id": "21", "egress_node_id": "5"}})
            prod = FakeClient(
                accounts={("grok_build", email): {"id": "201"}},
                nodes=[{"id": "9", "name": "pool-DE-AS1-sidANY", "scope": "grok_build", "enabled": True, "proxyConfigured": True}],
            )
            with mock.patch.object(pp, "Grok2APIClient", make_fake_grok2api_class(staging, prod)):
                result = pp.execute_prod_push(
                    repo,
                    record_id,
                    email,
                    self._config(
                        prod_push_egress_guard=False,
                        grok2api_auth_dir=os.path.join(tmp, "auth"),
                    ),
                )
            build = result["domains"]["grok_build"]
            self.assertEqual(build["status"], "pushed")
            self.assertFalse(build["matched"])
            # 缺 web 授权文件 → failed
            self.assertEqual(result["domains"]["grok_web"]["status"], "failed")
            self.assertEqual(result["status"], "partial")

    def test_skipped_when_disabled_or_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, record_id = self._repository(tmp)
            result = pp.execute_prod_push(repo, record_id, "a@b.com", self._config(prod_push_enabled=False))
            self.assertEqual(result["status"], "skipped")
            result = pp.execute_prod_push(repo, record_id, "a@b.com", self._config(prod_grok2api_remote_password=""))
            self.assertEqual(result["status"], "skipped")


def _make_test_pool(**kwargs):
    defaults = dict(
        mode="pool",
        urls=["hk-01 | http://a:1", "us-02 | http://b:2"],
        selection="round_robin",
        sticky_scope="none",
        cooldown_seconds=600,
    )
    defaults.update(kwargs)
    return proxy_pool.ProxyPool(**defaults)


class RiskResponseTests(unittest.TestCase):
    def _repo_with_record(self, tmp, extra):
        repo = RegistrationRepository(os.path.join(tmp, "rr.sqlite3"))
        record_id = repo.add_result(
            {"batch_id": "b", "email": "victim@example.com", "status": "success", "success": True, "extra": extra}
        )
        return repo, record_id

    def _payload(self):
        return {
            "degraded": True,
            "verdict": "degraded",
            "monitor_status": "high_risk",
            "risk_reasons": ["连续降智 3 次"],
        }

    def test_cooldown_pool_node_and_record_exit_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = _make_test_pool(sticky_scope="task")
            proxy_pool.configure_proxy_pool(lambda: pool, lambda: ("sig",))
            repo, record_id = self._repo_with_record(
                tmp,
                {
                    "exit_ip": "203.0.113.7",
                    "pool_node": {"entry": "hk-01 | http://a:1", "egress_ip": "203.0.113.7"},
                },
            )
            result = pp.apply_risk_response(
                repo, self._payload(), record_id, "victim@example.com", {}
            )
            self.assertEqual(result["status"], "processed")
            self.assertTrue(result["cooldown_applied"])
            self.assertEqual(result["pool_node"], "hk-01 | http://a:1")
            self.assertTrue(result["exit_ip_recorded"])
            self.assertEqual(pool._nodes["hk-01 | http://a:1"].status, "cooldown")

    def test_legacy_record_only_records_exit_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            proxy_pool.configure_proxy_pool(lambda: _make_test_pool(sticky_scope="task"), lambda: ("sig",))
            repo, record_id = self._repo_with_record(tmp, {"exit_ip": "198.51.100.9"})
            result = pp.apply_risk_response(
                repo, self._payload(), record_id, "victim@example.com", {}
            )
            self.assertFalse(result["cooldown_applied"])
            self.assertTrue(result["exit_ip_recorded"])
            self.assertIn("历史账号", result["error"])

    def test_missing_exit_ip_still_attempts_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = _make_test_pool(sticky_scope="task")
            proxy_pool.configure_proxy_pool(lambda: pool, lambda: ("sig",))
            repo, record_id = self._repo_with_record(
                tmp, {"pool_node": {"entry": "hk-01 | http://a:1"}}
            )
            result = pp.apply_risk_response(
                repo, self._payload(), record_id, "victim@example.com", {}
            )
            self.assertTrue(result["cooldown_applied"])
            self.assertFalse(result["exit_ip_recorded"])
            self.assertIn("出口 IP", result["error"])


class OutboxTests(unittest.TestCase):
    def test_enqueue_claim_finish_flow(self):
        import json as _json
        import sqlite3 as _sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            repo = RegistrationRepository(os.path.join(tmp, "r.sqlite3"))
            record_id = repo.add_result(
                {"batch_id": "b", "email": "u@x.com", "status": "success", "success": True}
            )
            event = repo.enqueue_prod_push(record_id, "u@x.com")
            self.assertEqual(event["status"], "pending")
            again = repo.enqueue_prod_push(record_id, "u@x.com")
            self.assertEqual(again["event_id"], event["event_id"])

            claimed = repo.claim_prod_push()
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["registration_id"], record_id)
            self.assertIsNone(repo.claim_prod_push())

            repo.finish_prod_push(event["event_id"], error="boom", delivered=False)
            # 快进退避计时，模拟等待到期
            with _sqlite3.connect(os.path.join(tmp, "r.sqlite3")) as conn:
                conn.execute("UPDATE prod_push_outbox SET next_attempt_at = 0")
            row = repo.claim_prod_push()
            self.assertIsNotNone(row)

            repo.finish_prod_push(event["event_id"], delivered=True)
            self.assertIsNone(repo.claim_prod_push())

            saved = repo.save_prod_push_result(record_id, {"status": "pushed", "domains": {}})
            extra = _json.loads(saved["extra_json"])
            self.assertEqual(extra["prod_push"]["status"], "pushed")

    def test_retry_exhausted_marks_failed(self):
        import sqlite3 as _sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            repo = RegistrationRepository(os.path.join(tmp, "r2.sqlite3"))
            record_id = repo.add_result(
                {"batch_id": "b", "email": "f@x.com", "status": "success", "success": True}
            )
            event = repo.enqueue_prod_push(record_id, "f@x.com")
            db_path = os.path.join(tmp, "r2.sqlite3")
            # 直接耗尽重试次数
            for _ in range(10):
                claimed = repo.claim_prod_push()
                if claimed is None:
                    with _sqlite3.connect(db_path) as conn:
                        conn.execute("UPDATE prod_push_outbox SET next_attempt_at = 0")
                    claimed = repo.claim_prod_push()
                if claimed is None:
                    break
                repo.finish_prod_push(event["event_id"], error="down", delivered=False)
            with _sqlite3.connect(db_path) as conn:
                status = conn.execute(
                    "SELECT status FROM prod_push_outbox WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchone()[0]
            self.assertEqual(status, "failed")
            self.assertIsNone(repo.claim_prod_push())


def json_loads(text):
    import json

    return json.loads(text or "{}")


if __name__ == "__main__":
    unittest.main()
