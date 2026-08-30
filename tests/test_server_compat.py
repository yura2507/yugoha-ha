import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "yugoha"


class FakeSocket:
    def __init__(self):
        self.events = []

    def send(self, raw):
        self.events.append(raw)


class ServerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data_dir = Path(self.temp.name)
        self._create_legacy_database()
        os.environ["YUGOHA_DATA_DIR"] = str(self.data_dir)
        sys.path.insert(0, str(APP_DIR))
        sys.modules.pop("app", None)
        self.server = importlib.import_module("app")
        self.client = self.server.app.test_client()

    def tearDown(self):
        sys.modules.pop("app", None)
        if str(APP_DIR) in sys.path:
            sys.path.remove(str(APP_DIR))
        os.environ.pop("YUGOHA_DATA_DIR", None)
        self.temp.cleanup()

    def _create_legacy_database(self):
        with sqlite3.connect(self.data_dir / "yugoha.sqlite") as conn:
            conn.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO meta VALUES ('sync_version', 0)")
            conn.execute(
                """CREATE TABLE devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
                    token TEXT NOT NULL, secret TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            conn.execute(
                "INSERT INTO devices VALUES (1, 'old-client', 'Old phone', 'old-token', 'old-secret', 'now', 'now')"
            )

    def _register(self, client_id, token, name):
        return self.client.post(
            "/api/register",
            json={
                "pair_code": self.server.STATE["pair_code"],
                "client_id": client_id,
                "fcm_token": token,
                "name": name,
            },
        )

    def test_migrates_existing_database_and_old_client_flow(self):
        with self.server.db() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(devices)")
            }
            old = conn.execute("SELECT recipient FROM devices WHERE id=1").fetchone()
        self.assertIn("recipient", columns)
        self.assertIsNone(old["recipient"])

        registered = self._register("old-client", "new-token", "Old phone")
        self.assertEqual(registered.status_code, 200)
        registration = registered.get_json()
        self.assertEqual(registration["device_id"], 1)
        self.assertIn("server_id", registration)
        self.assertIn("server_name", registration)

        synced = self.client.post(
            "/api/sync",
            headers={
                "X-YuGoHA-Device": "1",
                "X-YuGoHA-Secret": registration["device_secret"],
            },
            json={"cursor": 0, "read_ids": [], "delete_ids": []},
        )
        self.assertEqual(synced.status_code, 200)
        self.assertTrue(synced.get_json()["ok"])

        health = self.client.get("/api/health").get_json()
        self.assertEqual(health["version"], "0.5.0")

    def test_send_without_recipient_reaches_all_devices(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        sent_tokens = []

        with patch.object(self.server, "ensure_firebase", return_value=True), patch.object(
            self.server.messaging, "send", side_effect=lambda msg: sent_tokens.append(msg.token)
        ):
            response = self.client.post(
                "/api/message",
                headers={"X-YuGoHA-Key": self.server.STATE["api_key"]},
                json={"message": "legacy broadcast"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["delivered"], 2)
        self.assertCountEqual(sent_tokens, ["old-token", "token-2"])
        self.assertTrue(second["ok"])

    def test_recipient_routes_fcm_and_websocket_and_unknown_is_safe(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        with self.server.db() as conn:
            conn.execute("UPDATE devices SET recipient='yura' WHERE id=1")
            conn.execute("UPDATE devices SET recipient='other' WHERE id=?", (second["device_id"],))

        first_ws = FakeSocket()
        second_ws = FakeSocket()
        self.server.ws_clients[1] = {first_ws}
        self.server.ws_clients[second["device_id"]] = {second_ws}
        sent_tokens = []

        with patch.object(self.server, "ensure_firebase", return_value=True), patch.object(
            self.server.messaging, "send", side_effect=lambda msg: sent_tokens.append(msg.token)
        ):
            response = self.client.post(
                "/api/message",
                headers={"X-YuGoHA-Key": self.server.STATE["api_key"]},
                json={"message": "private", "recipient": "yura"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["delivered"], 1)
        self.assertEqual(sent_tokens, ["old-token"])
        self.assertEqual(len(first_ws.events), 1)
        self.assertEqual(second_ws.events, [])

        unknown = self.client.post(
            "/api/message",
            headers={"X-YuGoHA-Key": self.server.STATE["api_key"]},
            json={"message": "nobody", "recipient": "unknown"},
        )
        self.assertEqual(unknown.status_code, 200)
        self.assertTrue(unknown.get_json()["ok"])
        self.assertEqual(unknown.get_json()["delivered"], 0)


if __name__ == "__main__":
    unittest.main()
