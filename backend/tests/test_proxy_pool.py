# -*- coding: utf-8 -*-
"""proxy_pool 单元测试：模式/选择/粘性租约/健康管理/持久化/线程绑定。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from backend.integrations import proxy_pool as pp
from backend.integrations.proxy_pool import (
    MODE_POOL,
    MODE_STICKY_TEMPLATE,
    NodeState,
    ProxyConfigError,
    ProxyPool,
    ProxyPoolExhausted,
    build_pool_from_config,
    node_key,
)


def make_pool(**kwargs) -> ProxyPool:
    defaults = dict(
        mode=MODE_POOL,
        urls=["http://a:1", "http://b:2", "http://c:3"],
        selection="round_robin",
        sticky_scope="none",
        cooldown_seconds=0,
    )
    defaults.update(kwargs)
    return ProxyPool(**defaults)


class BuildFromConfigTests(unittest.TestCase):
    def test_mode_autodetect_static(self):
        pool = build_pool_from_config(lambda key, default=None: {"proxy": "http://p:1"}.get(key, default))
        self.assertEqual(pool.mode, "static")
        self.assertEqual(pool.urls, ["http://p:1"])

    def test_mode_autodetect_pool_by_multiline(self):
        pool = build_pool_from_config(
            lambda key, default=None: {"proxy": "http://a:1\nhttp://b:2"}.get(key, default)
        )
        self.assertEqual(pool.mode, MODE_POOL)
        self.assertEqual(pool.urls, ["http://a:1", "http://b:2"])

    def test_mode_autodetect_sticky_template(self):
        pool = build_pool_from_config(
            lambda key, default=None: {"proxy": "http://u-{account}:1"}.get(key, default)
        )
        self.assertEqual(pool.mode, MODE_STICKY_TEMPLATE)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ProxyConfigError):
            build_pool_from_config(
                lambda key, default=None: {"proxy": "http://a:1", "proxy_mode": "socks"}.get(key, default)
            )

    def test_invalid_selection_raises(self):
        with self.assertRaises(ProxyConfigError):
            build_pool_from_config(
                lambda key, default=None: {"proxy": "http://a:1", "proxy_selection": "magic"}.get(key, default)
            )

    def test_invalid_sticky_scope_raises(self):
        with self.assertRaises(ProxyConfigError):
            build_pool_from_config(
                lambda key, default=None: {"proxy": "http://a:1", "proxy_sticky_scope": "global"}.get(key, default)
            )

    def test_pool_requires_entries(self):
        with self.assertRaises(ProxyConfigError):
            build_pool_from_config(
                lambda key, default=None: {"proxy": "", "proxy_mode": "pool"}.get(key, default)
            )

    def test_proxy_file_merged(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("http://f:1\n\nhttp://f:2\n")
            path = f.name
        try:
            config = {"proxy": "http://a:1\nhttp://a:1", "proxy_file": path}
            pool = build_pool_from_config(
                lambda key, default=None: config.get(key, default),
                file_read=lambda p: open(p, encoding="utf-8").read(),
            )
            self.assertEqual(pool.urls, ["http://a:1", "http://f:1", "http://f:2"])
        finally:
            os.unlink(path)

    def test_invalid_pool_entry_raises(self):
        with self.assertRaises(ProxyConfigError):
            build_pool_from_config(
                lambda key, default=None: {"proxy": "http://ok:1\nftp://bad:2"}.get(key, default)
            )


class SelectionTests(unittest.TestCase):
    def test_round_robin_order(self):
        pool = make_pool()
        # sticky_scope=none：每次选取轮转
        picked = [pool.resolve(f"s{i}") for i in range(3)]
        self.assertEqual(picked, ["http://a:1", "http://b:2", "http://c:3"])

    def test_least_used_prefers_low_usage(self):
        pool = make_pool(selection="least_used")
        first = pool.resolve("s")
        pool._usage[first] = 10
        self.assertEqual(pool.resolve("t"), "http://b:2")

    def test_sticky_lease_keeps_node(self):
        pool = make_pool(sticky_scope="task")
        first = pool.resolve("w1")
        for _ in range(3):
            self.assertEqual(pool.resolve("w1"), first)
        self.assertNotEqual(pool.resolve("w2"), first)

    def test_release_allows_repick(self):
        pool = make_pool(sticky_scope="task", selection="least_used")
        first = pool.resolve("w1")
        pool.release("w1")
        pool._usage[first] = 99
        self.assertNotEqual(pool.resolve("w1"), first)

    def test_exhausted_when_no_healthy(self):
        pool = make_pool(cooldown_seconds=600)
        for url in pool.urls:
            pool.report_failure(url, reason="x")
        with self.assertRaises(ProxyPoolExhausted):
            pool.resolve("s")

    def test_cooldown_expiry_restores_node(self):
        pool = make_pool(cooldown_seconds=600)
        pool.report_failure("http://a:1", reason="risk")
        now = time.time()
        pool._nodes["http://a:1"].cooldown_until = now - 1
        self.assertEqual(pool._status_of("http://a:1", time.time()), "healthy")


class ProbeTests(unittest.TestCase):
    def test_probe_on_pick_marks_unreachable_and_moves_on(self):
        calls = []

        def probe(url):
            calls.append(url)
            return {"ok": url != "http://b:2", "egress_ip": "1.2.3.4" if url != "http://b:2" else "", "latency_ms": 5, "error": "" if url != "http://b:2" else "down"}

        pool = make_pool(urls=["http://b:2", "http://a:1"], probe=probe)
        self.assertEqual(pool.resolve("s"), "http://a:1")
        self.assertEqual(pool._nodes["http://b:2"].status, "unreachable")
        self.assertEqual(len(calls), 2)

    def test_probe_all_fail_raises(self):
        pool = make_pool(
            cooldown_seconds=600,
            probe=lambda url: {"ok": False, "egress_ip": "", "latency_ms": None, "error": "down"},
        )
        with self.assertRaises(ProxyPoolExhausted):
            pool.resolve("s")

    def test_probe_once_per_batch(self):
        calls = []

        def probe(url):
            calls.append(url)
            return {"ok": True, "egress_ip": "", "latency_ms": None, "error": ""}

        pool = make_pool(sticky_scope="task", probe=probe, probe_once_per_batch=True)
        pool.probe_all()
        pool.mark_batch_probed()
        before = len(calls)
        pool.resolve("w1")
        pool.resolve("w1")
        self.assertEqual(len(calls), before)
        self.assertEqual(pool._nodes["http://a:1"].status, "healthy")

    def test_probe_all_stats(self):
        def probe(url):
            return {"ok": url != "http://b:2", "egress_ip": "", "latency_ms": None, "error": ""}

        pool = make_pool(probe=probe)
        stats = pool.probe_all()
        self.assertEqual(stats, {"total": 3, "healthy": 2, "unreachable": 1})

    def test_probe_node_renders_credentials(self):
        seen = []

        def probe(url):
            seen.append(url)
            return {"ok": True, "egress_ip": "", "latency_ms": None, "error": ""}

        pool = make_pool(urls=["http://a:1"], username="u", password="p w", probe=probe)
        pool.probe_node("http://a:1")
        self.assertEqual(seen, ["http://u:p%20w@a:1"])


class HealthTests(unittest.TestCase):
    def test_report_failure_and_success(self):
        pool = make_pool(cooldown_seconds=600)
        pool.report_failure("http://a:1", reason="risk")
        self.assertEqual(pool._nodes["http://a:1"].status, "cooldown")
        pool.report_success("http://a:1", egress_ip="1.1.1.1", latency_ms=42)
        state = pool._nodes["http://a:1"]
        self.assertEqual(state.status, "healthy")
        self.assertEqual(state.egress_ip, "1.1.1.1")
        self.assertEqual(state.latency_ms, 42)

    def test_flagged_ip_skipped_and_isolated(self):
        pool = make_pool(
            urls=["http://a:1", "http://b:2"],
            sticky_scope="task",
            ip_flagged=lambda ip: ip == "9.9.9.9",
        )
        pool.report_success("http://a:1", egress_ip="9.9.9.9")
        self.assertEqual(pool.resolve("w1"), "http://b:2")
        self.assertEqual(pool._nodes["http://a:1"].status, "flagged")
        self.assertEqual(pool.healthy_count(), 1)

    def test_clear_cooldown_resets_flagged(self):
        pool = make_pool()
        pool.report_success("http://a:1", egress_ip="9.9.9.9")
        pool._nodes["http://a:1"].status = "flagged"
        self.assertTrue(pool.clear_cooldown("http://a:1"))
        self.assertEqual(pool._nodes["http://a:1"].status, "healthy")

    def test_node_list_and_key_lookup(self):
        pool = make_pool()
        nodes = pool.node_list()
        self.assertEqual([n["url"] for n in nodes], pool.urls)
        self.assertEqual([n["key"] for n in nodes], [node_key(u) for u in pool.urls])
        self.assertEqual(pool.find_url_by_key(node_key("http://b:2")), "http://b:2")
        self.assertEqual(pool.find_url_by_key("nope"), "")

    def test_state_persisted_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            pool = make_pool(cooldown_seconds=600, state_file=state_file)
            pool.report_failure("http://a:1", reason="risk")
            self.assertTrue(os.path.exists(state_file))
            restored = make_pool(cooldown_seconds=600, state_file=state_file)
            self.assertEqual(restored._status_of("http://a:1", time.time()), "cooldown")

    def test_add_remove_clear(self):
        pool = make_pool()
        result = pool.add_urls(["http://d:4", "http://a:1", "not a url"])
        self.assertEqual(result["added"], ["http://d:4"])
        self.assertEqual(result["invalid"], ["not a url"])
        self.assertTrue(pool.remove_url("http://d:4"))
        self.assertGreaterEqual(pool.clear(), 3)
        self.assertTrue(pool.empty())


class TemplateTests(unittest.TestCase):
    def test_account_placeholder_expansion(self):
        pool = make_pool(mode=MODE_STICKY_TEMPLATE, urls=["http://u-{account}:1"])
        self.assertEqual(pool.resolve("acc-42"), "http://u-acc-42:1")

    def test_email_placeholder_expansion(self):
        pool = make_pool(mode=MODE_STICKY_TEMPLATE, urls=["http://u-{email}:1"])
        self.assertEqual(pool.resolve("s", email="User.Name@example.com"), "http://u-username:1")


class CredentialsTests(unittest.TestCase):
    def test_merge_credentials(self):
        pool = make_pool(username="u", password="p@w")
        self.assertEqual(pool.render("http://a:1"), "http://u:p%40w@a:1")

    def test_url_credentials_win(self):
        pool = make_pool(username="u", password="p")
        self.assertEqual(pool.render("http://own:p@a:1"), "http://own:p@a:1")

    def test_ipv6_host_bracketed(self):
        pool = make_pool(username="u", password="p")
        self.assertEqual(pool.render("http://[::1]:8080"), "http://u:p@[::1]:8080")


class SingletonTests(unittest.TestCase):
    def setUp(self):
        pp.release_task()
        pp.configure_proxy_pool(lambda: ProxyPool("static", []), None)

    def tearDown(self):
        pp.release_task()
        pp.configure_proxy_pool(lambda: ProxyPool("static", []), None)

    def test_bind_and_release(self):
        pool = make_pool(sticky_scope="task", cooldown_seconds=600)
        pp.configure_proxy_pool(lambda: pool, lambda: ("sig",))
        rendered = pp.bind_task("w1")
        self.assertEqual(rendered, "http://a:1")
        self.assertEqual(pp.current_proxy_url(), "http://a:1")
        self.assertEqual(pp.current_raw_url(), "http://a:1")
        self.assertEqual(pp.current_scope_key(), "w1")
        pool.report_failure("http://a:1", reason="risk")
        self.assertEqual(pp.current_node_status(), "cooldown")
        changed = pp.rebind_if_unhealthy()
        self.assertTrue(changed)
        self.assertNotEqual(pp.current_raw_url(), "http://a:1")
        self.assertEqual(pp.current_node_status(), "healthy")
        pp.release_task()
        self.assertEqual(pp.current_proxy_url(), "")

    def test_rebind_noop_when_healthy(self):
        pool = make_pool(sticky_scope="task")
        pp.configure_proxy_pool(lambda: pool, lambda: ("sig",))
        pp.bind_task("w1")
        self.assertFalse(pp.rebind_if_unhealthy())
        self.assertEqual(pp.current_raw_url(), "http://a:1")

    def test_fallback_static_vs_pool(self):
        static_pool = ProxyPool("static", ["http://cfg:1"], username="u", password="p")
        pp.configure_proxy_pool(lambda: static_pool, None)
        self.assertEqual(pp.fallback_proxy_url(), "http://u:p@cfg:1")
        pp.configure_proxy_pool(lambda: make_pool(sticky_scope="task"), None)
        self.assertEqual(pp.fallback_proxy_url(), "")

    def test_config_error_falls_back_to_empty(self):
        def broken():
            raise ProxyConfigError("bad config")

        pp.configure_proxy_pool(broken, lambda: ("sig",))
        self.assertEqual(pp.pool_error(), "bad config")
        self.assertEqual(pp.fallback_proxy_url(), "")
        self.assertTrue(pp.get_pool().empty())

    def test_describe_pool_shape(self):
        pp.configure_proxy_pool(lambda: make_pool(sticky_scope="task"), None)
        info = pp.describe_pool()
        self.assertEqual(info["mode"], MODE_POOL)
        self.assertEqual(info["count"], 3)
        self.assertEqual(len(info["nodes"]), 3)
        self.assertIn("key", info["nodes"][0])


class ThreadScopeTests(unittest.TestCase):
    def test_bindings_are_thread_local(self):
        pp.release_task()
        pool = make_pool(sticky_scope="task")
        pp.configure_proxy_pool(lambda: pool, lambda: ("sig",))
        rendered = {}
        lock = threading.Lock()

        def worker(name):
            value = pp.bind_task(name)
            with lock:
                rendered[name] = value

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(rendered.values())), 3)
        self.assertEqual(pp.current_proxy_url(), "")


class PersistenceHardeningTests(unittest.TestCase):
    def test_load_state_skips_only_corrupt_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            good = make_pool(cooldown_seconds=600, state_file=state_file)
            good.report_failure("http://a:1", reason="risk")
            payload = json.loads(open(state_file, encoding="utf-8").read())
            payload["http://b:2"] = {"status": "healthy", "cooldown_until": "not-a-number"}
            payload["http://ghost:9"] = {"status": "cooldown"}
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(payload, f)

            restored = make_pool(cooldown_seconds=600, state_file=state_file)
            self.assertEqual(restored._status_of("http://a:1", time.time()), "cooldown")
            self.assertEqual(restored._status_of("http://b:2", time.time()), "healthy")
            self.assertNotIn("http://ghost:9", restored._nodes)

    def test_concurrent_persist_keeps_state_file_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = os.path.join(tmp, "state.json")
            pool = make_pool(cooldown_seconds=600, state_file=state_file)

            def worker(index: int):
                pool.report_failure(pool.urls[index % len(pool.urls)], reason=f"w{index}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            data = json.loads(open(state_file, encoding="utf-8").read())
            self.assertEqual(len(data), 3)
            for state in data.values():
                self.assertIn(state["status"], ("cooldown", "healthy"))


if __name__ == "__main__":
    unittest.main()
