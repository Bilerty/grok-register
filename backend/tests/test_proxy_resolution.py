import unittest
from unittest import mock

from backend.integrations.proxy import (
    parse_http_proxy_url,
    redact_proxy_text,
    redact_proxy_url,
    resolve_proxy_url,
    validate_http_proxy_url,
)


class DockerProxyResolutionTests(unittest.TestCase):
    def test_localhost_proxy_maps_to_docker_host(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://127.0.0.1:7897"),
                "http://host.docker.internal:7897",
            )

    def test_credentials_are_preserved(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("socks5://user:pass@localhost:7897"),
                "socks5://user:pass@host.docker.internal:7897",
            )

    def test_encoded_http_credentials_are_preserved_during_host_rewrite(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://user%40mail:p%40ss%3Aword@localhost:7897"),
                "http://user%40mail:p%40ss%3Aword@host.docker.internal:7897",
            )

    def test_regular_proxy_is_unchanged(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://proxy.example.com:7897"),
                "http://proxy.example.com:7897",
            )


class HttpProxyParsingTests(unittest.TestCase):
    def test_authenticated_http_proxy_is_split_for_camoufox(self):
        self.assertEqual(
            parse_http_proxy_url("http://user:password@proxy.example.com:8080"),
            {
                "server": "http://proxy.example.com:8080",
                "username": "user",
                "password": "password",
            },
        )

    def test_percent_encoded_credentials_are_decoded(self):
        self.assertEqual(
            parse_http_proxy_url(
                "https://user%40mail.example:p%40ss%3Aword@proxy.example.com:8443"
            ),
            {
                "server": "https://proxy.example.com:8443",
                "username": "user@mail.example",
                "password": "p@ss:word",
            },
        )

    def test_original_encoded_url_is_retained_for_http_clients(self):
        proxy = "http://user%40mail:p%40ss@proxy.example.com:8080"
        self.assertEqual(validate_http_proxy_url(proxy), proxy)

    def test_invalid_percent_encoding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "百分号编码"):
            validate_http_proxy_url("http://user:bad%ZZ@proxy.example.com:8080")

    def test_unencoded_path_character_in_credentials_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "百分号编码"):
            validate_http_proxy_url("http://user:bad/word@proxy.example.com:8080")

    def test_proxy_credentials_are_redacted_for_display_and_log_text(self):
        proxy = "http://user%40mail:p%40ss@proxy.example.com:8080"
        self.assertEqual(
            redact_proxy_url(proxy),
            "http://***:***@proxy.example.com:8080",
        )
        message = redact_proxy_text(f"request failed via {proxy}")
        self.assertNotIn("user%40mail", message)
        self.assertNotIn("p%40ss", message)
        self.assertIn("http://***:***@proxy.example.com:8080", message)

        malformed = redact_proxy_text(
            "failed via http://user:raw/secret@proxy.example.com:8080"
        )
        self.assertNotIn("raw/secret", malformed)


class SocksProxySchemeTests(unittest.TestCase):
    """socks5 / socks5h 协议支持：校验、脱敏、浏览器 proxy dict。"""

    def test_socks_urls_pass_validation(self):
        for url in (
            "socks5://gw.example.com:1080",
            "socks5h://user:pass@gw.example.com:1080",
            "socks5h://user%40mail:p%40ss@gw.example.com:1080",
        ):
            self.assertEqual(validate_http_proxy_url(url), url)

    def test_unsupported_schemes_still_rejected(self):
        for url in ("ftp://gw:21", "socks4://gw:1080", "socks5://gw:1080/path"):
            with self.assertRaises(ValueError):
                validate_http_proxy_url(url)

    def test_socks_credentials_are_redacted(self):
        proxy = "socks5h://user%40mail:p%40ss@gw.example.com:1080"
        self.assertEqual(
            redact_proxy_url(proxy),
            "socks5h://***:***@gw.example.com:1080",
        )

    def test_browser_proxy_dict_for_socks_splits_credentials(self):
        from backend.automation.session import _build_camoufox_proxy

        result = _build_camoufox_proxy(
            "socks5h://user%40mail:p%40ss@gw.example.com:1080"
        )
        self.assertEqual(result["server"], "socks5h://gw.example.com:1080")
        self.assertEqual(result["username"], "user@mail")
        self.assertEqual(result["password"], "p@ss")

    def test_browser_proxy_dict_for_socks_without_auth(self):
        from backend.automation.session import _build_camoufox_proxy

        self.assertEqual(
            _build_camoufox_proxy("socks5://gw.example.com:1080"),
            {"server": "socks5://gw.example.com:1080"},
        )

    def test_socks_entry_accepted_by_proxy_pool(self):
        from backend.integrations import proxy_pool as pp

        config = {
            "proxy": "hk-socks | socks5h://user:pass@gw.example.com:1080",
            "proxy_mode": "pool",
        }
        pool = pp.build_pool_from_config(lambda key, default=None: config.get(key, default))
        self.assertEqual(pool.urls, ["hk-socks | socks5h://user:pass@gw.example.com:1080"])
        self.assertEqual(
            pool.render(pool.urls[0]),
            "socks5h://user:pass@gw.example.com:1080",
        )


if __name__ == "__main__":
    unittest.main()
