"""代理池 / 粘性代理框架测试。"""

import time
import unittest

from backend.integrations.proxy import build_proxy_url_with_credentials
from backend.integrations import proxy_pool as pp


def _config(**overrides):
    base = {
        "proxy": "",
        "proxy_mode": "",
        "proxy_selection": "round_robin",
        "proxy_sticky_scope": "task",
        "proxy_file": "",
        "proxy_username": "",
        "proxy_password": "",
    }
    base.update(overrides)
    return lambda key, default=None: base.get(key, default)


class BuildPoolTests(unittest.TestCase):

    def test_empty_config_gives_empty_static_pool(self):
        pool = pp.build_pool_from_config(_config())
        self.assertEqual(pool.mode, pp.MODE_STATIC)
        self.assertTrue(pool.empty())
        self.assertEqual(pool.resolve(), "")

    def test_single_proxy_is_static_by_default(self):
        pool = pp.build_pool_from_config(_config(proxy="http://127.0.0.1:7890"))
        self.assertEqual(pool.mode, pp.MODE_STATIC)
        self.assertEqual(pool.resolve(), "http://127.0.0.1:7890")

    def test_multiline_proxy_auto_detects_pool(self):
        proxy = "http://127.0.0.1:8001\nhttp://127.0.0.1:8002\n"
        pool = pp.build_pool_from_config(_config(proxy=proxy))
        self.assertEqual(pool.mode, pp.MODE_POOL)
        self.assertEqual(len(pool.urls), 2)

    def test_placeholder_auto_detects_sticky_template(self):
        proxy = "http://pool-{account}:secret@127.0.0.1:2260"
        pool = pp.build_pool_from_config(_config(proxy=proxy))
        self.assertEqual(pool.mode, pp.MODE_STICKY_TEMPLATE)
        self.assertEqual(
            pool.resolve(scope_key="reg-7"),
            "http://pool-reg-7:secret@127.0.0.1:2260",
        )

    def test_email_placeholder_uses_alnum_lower_local_part(self):
        proxy = "http://pool-{email}:secret@127.0.0.1:2260"
        pool = pp.build_pool_from_config(_config(proxy=proxy))
        self.assertEqual(
            pool.resolve(email="User.Name@example.com"),
            "http://pool-username:secret@127.0.0.1:2260",
        )

    def test_invalid_pool_entry_raises(self):
        with self.assertRaises(pp.ProxyConfigError):
            pp.build_pool_from_config(_config(proxy="socks5://bad\nhttp://ok:8080"))

    def test_invalid_mode_raises(self):
        with self.assertRaises(pp.ProxyConfigError):
            pp.build_pool_from_config(_config(proxy="http://ok:8080", proxy_mode="bogus"))


class SelectionTests(unittest.TestCase):

    def test_round_robin_cycles(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2", "p3"])
        seen = [pool.resolve() for _ in range(6)]
        self.assertEqual(seen[:3], ["p1", "p2", "p3"])
        self.assertEqual(seen[3:], ["p1", "p2", "p3"])

    def test_random_stays_within_pool(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2"], selection=pp.SELECTION_RANDOM)
        self.assertIn(pool.resolve(), {"p1", "p2"})

    def test_least_used_prefers_less_used(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2"], selection=pp.SELECTION_LEAST_USED)
        first = pool.resolve()
        second = pool.resolve()
        self.assertNotEqual(first, second)


class StickyLeaseTests(unittest.TestCase):

    def test_pool_scope_keeps_same_proxy(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2", "p3"])
        self.assertEqual(pool.resolve(scope_key="a"), pool.resolve(scope_key="a"))
        a = pool.resolve(scope_key="a")
        b = pool.resolve(scope_key="b")
        self.assertNotEqual(a, b)

    def test_release_frees_lease(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2", "p3"])
        a = pool.resolve(scope_key="a")
        pool.release("a")
        again = pool.resolve(scope_key="a")
        self.assertIn(again, {"p1", "p2", "p3"})

    def test_none_scope_ignores_leases(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2"], sticky_scope=pp.STICKY_SCOPE_NONE)
        picks = [pool.resolve(scope_key="a") for _ in range(4)]
        self.assertEqual(len(set(picks)), 2)


class CredentialOverrideTests(unittest.TestCase):

    def test_special_chars_are_encoded(self):
        url = build_proxy_url_with_credentials(
            "http://127.0.0.1:2260", username="user@name", password="p@ss:#%"
        )
        self.assertEqual(url, "http://user%40name:p%40ss%3A%23%25@127.0.0.1:2260")

    def test_override_replaces_existing_userinfo(self):
        url = build_proxy_url_with_credentials(
            "http://old:old@127.0.0.1:2260", username="new", password="pw"
        )
        self.assertEqual(url, "http://new:pw@127.0.0.1:2260")

    def test_empty_credentials_leave_url_unchanged(self):
        url = build_proxy_url_with_credentials("http://u:p@127.0.0.1:2260")
        self.assertEqual(url, "http://u:p@127.0.0.1:2260")

    def test_username_without_password_supported(self):
        url = build_proxy_url_with_credentials(
            "http://127.0.0.1:2260", username="onlyuser"
        )
        self.assertEqual(url, "http://onlyuser@127.0.0.1:2260")

    def test_pool_applies_raw_credentials_to_picks(self):
        pool = pp.build_pool_from_config(_config(
            proxy="http://127.0.0.1:8001\nhttp://127.0.0.1:8002",
            proxy_username="u@x",
            proxy_password="p#1",
        ))
        url = pool.render(pool.resolve(scope_key="t"))
        self.assertEqual(url, "http://u%40x:p%231@127.0.0.1:8001")


class TaskScopeTests(unittest.TestCase):

    def setUp(self):
        self._saved = (
            pp._build_pool, pp._config_signature, pp._pool, pp._pool_signature
        )
        pp.configure_proxy_pool(lambda: pp.build_pool_from_config(_config(
            proxy="http://pool-{account}:secret@127.0.0.1:2260",
        )))

    def tearDown(self):
        pp.release_task()
        pp._build_pool, pp._config_signature, pp._pool, pp._pool_signature = self._saved

    def test_bind_sets_current_scope(self):
        url = pp.bind_task(scope_key="reg-9")
        self.assertEqual(url, "http://pool-reg-9:secret@127.0.0.1:2260")
        self.assertEqual(pp.current_proxy_url(), url)
        pp.release_task()

    def test_release_clears_scope(self):
        pp.bind_task(scope_key="reg-9")
        pp.release_task()
        self.assertEqual(pp.current_scope_key(), "")


class HealthManagementTests(unittest.TestCase):

    def _pool(self, probe_results):
        calls = []
        def probe(url):
            calls.append(url)
            return probe_results[url]
        pool = pp.ProxyPool(
            pp.MODE_POOL,
            ["p1", "p2"],
            cooldown_seconds=60,
            probe=probe,
        )
        return pool, calls

    def test_probe_on_pick_fails_over_to_next_node(self):
        pool, calls = self._pool({
            "p1": {"ok": False, "error": "bad"},
            "p2": {"ok": True, "egress_ip": "1.2.3.4", "latency_ms": 120},
        })
        picked = pool.resolve(scope_key="t")
        self.assertEqual(picked, "p2")
        nodes = {n["url"]: n for n in pool.node_list()}
        self.assertEqual(nodes["p1"]["status"], pp.STATUS_UNREACHABLE)
        self.assertEqual(nodes["p2"]["status"], pp.STATUS_HEALTHY)
        self.assertEqual(nodes["p2"]["egress_ip"], "1.2.3.4")

    def test_probe_all_fail_raises_exhausted(self):
        pool, _ = self._pool({
            "p1": {"ok": False, "error": "bad"},
            "p2": {"ok": False, "error": "bad"},
        })
        with self.assertRaises(pp.ProxyPoolExhausted):
            pool.resolve(scope_key="t")

    def test_report_failure_puts_node_in_cooldown(self):
        pool, _ = self._pool({"p1": {"ok": True}, "p2": {"ok": True}})
        pool.report_failure("p1", reason="registration_risk")
        picks = [pool.resolve(scope_key=f"t{i}") for i in range(3)]
        self.assertTrue(all(pick == "p2" for pick in picks))
        nodes = {n["url"]: n for n in pool.node_list()}
        self.assertEqual(nodes["p1"]["status"], pp.STATUS_COOLDOWN)
        self.assertGreater(nodes["p1"]["cooldown_remaining"], 0)

    def test_cooldown_expires(self):
        pool, _ = self._pool({"p1": {"ok": True}, "p2": {"ok": True}})
        pool.report_failure("p1", reason="registration_risk")
        with pool._lock:
            pool._nodes["p1"].cooldown_until = time.time() - 1
        picks = [pool.resolve(scope_key=f"t{i}") for i in range(4)]
        self.assertIn("p1", picks)

    def test_clear_cooldown(self):
        pool, _ = self._pool({"p1": {"ok": True}, "p2": {"ok": True}})
        pool.report_failure("p1", reason="registration_risk")
        pool.clear_cooldown("p1")
        nodes = {n["url"]: n for n in pool.node_list()}
        self.assertEqual(nodes["p1"]["status"], pp.STATUS_HEALTHY)

    def test_report_success_records_egress(self):
        pool, _ = self._pool({"p1": {"ok": True}, "p2": {"ok": True}})
        pool.report_success("p1", egress_ip="9.8.7.6", latency_ms=88)
        nodes = {n["url"]: n for n in pool.node_list()}
        self.assertEqual(nodes["p1"]["egress_ip"], "9.8.7.6")
        self.assertEqual(nodes["p1"]["latency_ms"], 88)
        self.assertEqual(nodes["p1"]["status"], pp.STATUS_HEALTHY)

    def test_zero_cooldown_disables_cooling(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2"], cooldown_seconds=0)
        pool.report_failure("p1", reason="x")
        nodes = {n["url"]: n for n in pool.node_list()}
        self.assertNotEqual(nodes["p1"]["status"], pp.STATUS_COOLDOWN)


class PoolManagementTests(unittest.TestCase):

    def test_add_urls_dedupes_and_validates(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["http://127.0.0.1:8001"])
        result = pool.add_urls(["http://127.0.0.1:8002", "http://127.0.0.1:8001", "socks5://bad"])
        self.assertEqual(result["added"], ["http://127.0.0.1:8002"])
        self.assertEqual(result["invalid"], ["socks5://bad"])
        self.assertEqual(len(pool.url_list()), 2)

    def test_remove_url_clears_state_and_leases(self):
        pool = pp.ProxyPool(pp.MODE_POOL, ["p1", "p2"])
        pool.resolve(scope_key="a")
        self.assertTrue(pool.remove_url("p1"))
        self.assertNotIn("p1", pool.url_list())
        self.assertNotIn("p1", [n["url"] for n in pool.node_list()])
        self.assertFalse(pool.remove_url("p1"))


if __name__ == "__main__":
    unittest.main()
