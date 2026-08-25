"""access_token JWT bfs=0 正常，非零值为风控标记。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.integrations import auth_exchange
from backend.registration import engine
from backend.registration.store import RegistrationRepository
from backend.web import application as webapp


def fake_jwt(payload: dict) -> str:
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."


class AccessTokenBotRiskTests(unittest.TestCase):
    def test_decode_helpers_treat_every_nonzero_bfs_as_risk(self):
        token = fake_jwt({"sub": "user", "bfs": 1})
        self.assertEqual(auth_exchange.access_token_bfs(token), 1)
        self.assertTrue(auth_exchange.access_token_bot_risk(token))

        normal = fake_jwt({"sub": "user"})
        self.assertIsNone(auth_exchange.access_token_bfs(normal))
        self.assertFalse(auth_exchange.access_token_bot_risk(normal))

        string_flag = fake_jwt({"bfs": "1"})
        self.assertTrue(auth_exchange.access_token_bot_risk(string_flag))

        for value in (2, 3, -1, "2", "-1"):
            with self.subTest(value=value):
                self.assertTrue(auth_exchange.access_token_bot_risk(fake_jwt({"bfs": value})))

        for value in (0, "0"):
            with self.subTest(value=value):
                self.assertFalse(auth_exchange.access_token_bot_risk(fake_jwt({"bfs": value})))

    def test_add_sso_to_cpa_stores_bot_risk_on_result(self):
        original = dict(engine.config)
        g2a_dir = tempfile.mkdtemp()
        try:
            engine.config.update(
                {
                    "cpa_auto_add": True,
                    "cpa_auth_dir": "",
                    "cpa_remote_url": "",
                    "cpa_management_key": "",
                    "grok2api_auth_dir": g2a_dir,
                    "grok2api_auto_import": False,
                    "cpa_token_mode": "device_protocol",
                    "proxy": "",
                }
            )
            token = {
                "access_token": fake_jwt(
                    {
                        "sub": "acct",
                        "bfs": 1,
                        "exp": 9999999999,
                    }
                ),
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            result = {}
            with (
                mock.patch.object(engine._s2cpa, "sso_to_token", return_value=token),
                mock.patch.object(
                    engine._s2cpa,
                    "write_grok2api_auth",
                    return_value=Path(g2a_dir) / "g2a-fixture.json",
                ),
                mock.patch.object(
                    engine._grok2api.Grok2APIClient,
                    "is_configured",
                    return_value=False,
                ),
            ):
                ok = engine.add_sso_to_cpa("sso-token", email="risk@example.com", result_out=result)
            self.assertTrue(ok)
            self.assertTrue(result.get("bot_risk"))
            self.assertEqual(str(result.get("bfs")), "1")
        finally:
            engine.config.clear()
            engine.config.update(original)

    def test_repository_and_serialize_expose_bot_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            row_id = store.add_result(
                {
                    "email": "risk@example.com",
                    "status": "success",
                    "bot_risk": True,
                    "bfs": 1,
                }
            )
            rows = store.get_results_by_ids([row_id])
            self.assertEqual(rows[0]["bot_risk"], 1)
            self.assertEqual(str(rows[0]["bfs"]), "1")

            with mock.patch.object(webapp, "_gr") as gr_mock:
                gr_mock.return_value.config = {
                    "cpa_auth_dir": str(Path(tmp) / "cpa"),
                    "grok2api_auth_dir": str(Path(tmp) / "g2a"),
                }
                with (
                    mock.patch.object(webapp, "_find_account_auth_file", side_effect=FileNotFoundError),
                    mock.patch.object(webapp, "_find_account_sso_file", side_effect=FileNotFoundError),
                    mock.patch(
                        "backend.integrations.grok2api_client.Grok2APIClient.is_configured",
                        return_value=False,
                    ),
                ):
                    item = webapp._serialize_record(rows[0])
            self.assertTrue(item["bot_risk"])
            self.assertEqual(item["bfs"], "1")

    def test_serialize_exposes_detailed_sso_risk_result(self):
        record = {
            "id": 1,
            "extra_json": json.dumps(
                {
                    "sso_risk_check": {
                        "verdict": "clean",
                        "bot_flag_source": 0,
                        "valid_session": True,
                    }
                }
            ),
        }
        with mock.patch.object(webapp, "_gr") as gr_mock:
            gr_mock.return_value.config = {}
            with (
                mock.patch.object(webapp, "_find_account_auth_file", side_effect=FileNotFoundError),
                mock.patch.object(webapp, "_find_account_sso_file", side_effect=FileNotFoundError),
                mock.patch(
                    "backend.integrations.grok2api_client.Grok2APIClient.is_configured",
                    return_value=False,
                ),
            ):
                item = webapp._serialize_record(record)

        self.assertEqual(item["sso_risk_check"]["bot_flag_source"], 0)
        self.assertEqual(item["sso_risk_check"]["verdict"], "clean")

    def test_serialize_treats_any_nonzero_bfs_as_risk(self):
        record = {"id": 1, "bot_risk": 0, "bfs": "4", "extra_json": "{}"}
        with mock.patch.object(webapp, "_gr") as gr_mock:
            gr_mock.return_value.config = {}
            with (
                mock.patch.object(webapp, "_find_account_auth_file", side_effect=FileNotFoundError),
                mock.patch.object(webapp, "_find_account_sso_file", side_effect=FileNotFoundError),
                mock.patch(
                    "backend.integrations.grok2api_client.Grok2APIClient.is_configured",
                    return_value=False,
                ),
            ):
                item = webapp._serialize_record(record)

        self.assertTrue(item["bot_risk"])
        self.assertEqual(item["bfs"], "4")

    def test_migration_adds_bot_risk_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            import sqlite3
            from contextlib import closing

            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE registration_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_key TEXT UNIQUE,
                        batch_id TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT 'gui',
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        duration_seconds REAL NOT NULL DEFAULT 0,
                        email TEXT NOT NULL DEFAULT '',
                        password TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'failure',
                        success INTEGER NOT NULL DEFAULT 0,
                        provider TEXT NOT NULL DEFAULT '',
                        worker_id INTEGER NOT NULL DEFAULT 0,
                        cpa_enabled INTEGER NOT NULL DEFAULT 0,
                        cpa_status TEXT NOT NULL DEFAULT 'disabled',
                        auth_info TEXT NOT NULL DEFAULT '',
                        auth_path TEXT NOT NULL DEFAULT '',
                        failure_type TEXT NOT NULL DEFAULT '',
                        failure_reason TEXT NOT NULL DEFAULT '',
                        account_file TEXT NOT NULL DEFAULT '',
                        sso_saved INTEGER NOT NULL DEFAULT 0,
                        nsfw_status TEXT NOT NULL DEFAULT '',
                        extra_json TEXT NOT NULL DEFAULT '{}'
                    );
                    PRAGMA user_version = 1;
                    """
                )
                conn.execute(
                    """
                    INSERT INTO registration_results
                    (started_at, finished_at, email, status, success)
                    VALUES ('2026-08-01 00:00:00', '2026-08-01 00:00:01',
                            'old@example.com', 'success', 1)
                    """
                )
                conn.commit()

            store = RegistrationRepository(path)
            with closing(sqlite3.connect(path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(registration_results)")}
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 8)
            self.assertIn("bot_risk", columns)
            self.assertIn("bfs", columns)
            self.assertEqual(store.list_results()[0]["bot_risk"], 0)


if __name__ == "__main__":
    unittest.main()
