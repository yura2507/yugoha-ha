import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# Offline mode substitutes only external delivery adapters. HTTP routing, Flask
# test_client, SQLite migration and all production query/authorization code run
# unchanged. Normal CI uses the packages from yugoha/requirements.txt.
if os.environ.get("YUGOHA_TEST_OFFLINE_TRANSPORTS") == "1":
    import types

    class OfflineSock:
        def __init__(self, app):
            pass

        def route(self, path):
            return lambda handler: handler

    sock_module = types.ModuleType("flask_sock")
    sock_module.Sock = OfflineSock
    sys.modules["flask_sock"] = sock_module
    firebase = types.ModuleType("firebase_admin")
    firebase._apps = {}
    firebase.credentials = types.SimpleNamespace(Certificate=lambda value: value)
    firebase.messaging = types.SimpleNamespace(
        Message=lambda **values: types.SimpleNamespace(**values),
        AndroidConfig=lambda **values: types.SimpleNamespace(**values),
        send=lambda msg: (_ for _ in ()).throw(AssertionError("Unexpected FCM send")),
    )
    sys.modules["firebase_admin"] = firebase


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
            conn.execute(
                """CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5, created_at TEXT NOT NULL)"""
            )
            conn.execute(
                "INSERT INTO messages(title,message,created_at) VALUES('Legacy','Old shared history','now')"
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

    def _headers(self, device=None):
        return {
            "X-YuGoHA-Device": str(device["device_id"] if device else 1),
            "X-YuGoHA-Secret": device["device_secret"] if device else "old-secret",
        }

    def _sync(self, device=None, **payload):
        return self.client.post(
            "/api/sync", headers=self._headers(device), json={"cursor": 0, **payload}
        )

    def _recipient(self, device_id, recipient):
        with self.server.db() as conn:
            conn.execute("UPDATE devices SET recipient=? WHERE id=?", (recipient, device_id))

    def _ids(self, response):
        self.assertEqual(response.status_code, 200)
        return {item["id"] for item in response.get_json()["items"]}

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
        self.assertEqual(health["version"], "0.5.1")
        self.assertEqual(synced.get_json()["server_id"], health["server_id"])
        self.assertEqual(synced.get_json()["items"][0]["message"], "Old shared history")

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

    def test_targeted_history_is_private_and_audience_is_a_snapshot(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        self._recipient(1, "yura")
        self._recipient(second["device_id"], "other")
        private = self.server.create_message("Private", "only original yura phones", 5, "yura")
        unknown = self.server.create_message("Nobody", "no recipients", 5, "unknown")
        common = self.server.create_message("Common", "everyone", 5)

        # Renaming groups later never grants access to an earlier private message.
        self._recipient(1, "renamed")
        self._recipient(second["device_id"], "yura")
        third = self._register("third", "token-3", "New phone").get_json()
        self._recipient(third["device_id"], "yura")
        self.assertEqual(self._ids(self._sync()), {1, private["id"], common["id"]})
        self.assertEqual(self._ids(self._sync(second)), {1, common["id"]})
        self.assertEqual(self._ids(self._sync(third)), {1, common["id"]})
        self.assertNotIn(unknown["id"], self._ids(self._sync()))

        sent = []
        with patch.object(self.server, "ensure_firebase", return_value=True), patch.object(
            self.server.messaging, "send", side_effect=lambda msg: sent.append(msg)
        ):
            self.server.send_fcm_new(private, "yura")
        self.assertEqual([msg.token for msg in sent], ["old-token"])
        self.assertEqual(sent[0].data["server_id"], self.server.STATE["server_id"])

    def test_unauthorized_read_and_delete_batches_do_not_mutate_anything(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        self._recipient(1, "private")
        private = self.server.create_message("Private", "secret", 5, "private")
        common = self.server.create_message("Common", "shared", 5)
        for field in ["read_ids", "delete_ids"]:
            with self.subTest(field=field):
                denied = self._sync(second, **{field: [common["id"], private["id"]]})
                self.assertEqual(denied.status_code, 403)
                with self.server.db() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM device_reads").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT SUM(deleted) FROM messages").fetchone()[0], 0)

    def test_read_is_per_phone_delete_is_global_within_the_message_audience(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        third = self._register("third", "token-3", "Third phone").get_json()
        self._recipient(1, "family")
        self._recipient(second["device_id"], "family")
        self._recipient(third["device_id"], "other")
        private = self.server.create_message("Family", "shared family message", 5, "family")
        message_id = private["id"]
        first = self._sync(read_ids=[message_id]).get_json()
        self.assertTrue(next(x for x in first["items"] if x["id"] == message_id)["read"])
        other = self._sync(second).get_json()
        self.assertFalse(next(x for x in other["items"] if x["id"] == message_id)["read"])

        sockets = {i: FakeSocket() for i in [1, second["device_id"], third["device_id"]]}
        self.server.ws_clients.update({i: {ws} for i, ws in sockets.items()})
        sent = []
        with patch.object(self.server, "ensure_firebase", return_value=True), patch.object(
            self.server.messaging, "send", side_effect=lambda msg: sent.append(msg)
        ):
            deleted = self._sync(delete_ids=[message_id])
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["deleted_now"], [message_id])
        self.assertCountEqual([msg.token for msg in sent], ["old-token", "token-2"])
        for msg in sent:
            self.assertEqual(msg.data["type"], "yugoha_delete")
            self.assertEqual(msg.data["server_id"], self.server.STATE["server_id"])
            self.assertEqual(msg.data["server_name"], self.server.STATE["server_name"])
        for device_id in [1, second["device_id"]]:
            event = json.loads(sockets[device_id].events[0])
            self.assertEqual(event["type"], "delete")
            self.assertEqual(event["server_id"], self.server.STATE["server_id"])
        self.assertEqual(sockets[third["device_id"]].events, [])
        second_items = self._sync(second).get_json()["items"]
        self.assertTrue(next(x for x in second_items if x["id"] == message_id)["deleted"])
        self.assertNotIn(message_id, self._ids(self._sync(third)))

    def test_filtered_pagination_advances_without_skipping_visible_messages(self):
        self._recipient(1, "mine")
        visible = self.server.create_message("A", "visible", 5, "mine")
        hidden = self.server.create_message("B", "hidden", 5, "nobody")
        last = self.server.create_message("C", "visible too", 5)
        first_page = self.server.get_changes(1, 0, limit=1)
        self.assertEqual([x["id"] for x in first_page["items"]], [1])
        self.assertTrue(first_page["has_more"])
        second_page = self.server.get_changes(1, first_page["cursor"], limit=1)
        self.assertEqual([x["id"] for x in second_page["items"]], [visible["id"]])
        third_page = self.server.get_changes(1, second_page["cursor"], limit=1)
        self.assertEqual([x["id"] for x in third_page["items"]], [last["id"]])
        self.assertFalse(third_page["has_more"])
        self.assertGreaterEqual(third_page["cursor"], hidden["version"])
        self.assertEqual(self.server.get_changes(1, third_page["cursor"])["items"], [])

    def test_device_test_message_is_scoped_and_identifies_the_server(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        sent = []
        with patch.object(self.server, "ensure_firebase", return_value=True), patch.object(
            self.server.messaging, "send", side_effect=lambda msg: sent.append(msg)
        ):
            result = self.client.post("/test_device", data={"device_id": second["device_id"]})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].token, "token-2")
        self.assertEqual(sent[0].data["server_id"], self.server.STATE["server_id"])
        self.assertNotIn(int(sent[0].data["id"]), self._ids(self._sync()))
        self.assertIn(int(sent[0].data["id"]), self._ids(self._sync(second)))

    def test_server_name_option_updates_without_changing_identity(self):
        original_id = self.server.STATE["server_id"]
        self.server.OPTIONS_PATH.write_text(json.dumps({"server_name": "Дача"}), encoding="utf-8")
        state = self.server.load_state()
        self.assertEqual(state["server_id"], original_id)
        self.assertEqual(state["server_name"], "Дача")
        self.server.OPTIONS_PATH.write_text("{}", encoding="utf-8")
        self.assertEqual(self.server.load_state()["server_name"], "Дача")

    def test_unknown_ids_remain_idempotent_and_broadcast_delete_reaches_all(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        unknown = self._sync(read_ids=[99999], delete_ids=[99999])
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.get_json()["ack_delete"], [99999])
        sent = []
        with patch.object(self.server, "ensure_firebase", return_value=True), patch.object(
            self.server.messaging, "send", side_effect=lambda msg: sent.append(msg)
        ):
            response = self._sync(delete_ids=[1])
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual([msg.token for msg in sent], ["old-token", "token-2"])
        self.assertTrue(self._sync(second).get_json()["items"][0]["deleted"])

    def test_token_update_response_identifies_server(self):
        result = self.client.post(
            "/api/device/token", headers=self._headers(), json={"fcm_token": "refreshed-token"}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["server_id"], self.server.STATE["server_id"])
        with self.server.db() as conn:
            self.assertEqual(conn.execute("SELECT token FROM devices WHERE id=1").fetchone()[0], "refreshed-token")

    def test_repeated_migration_preserves_data_identity_and_audience(self):
        self._recipient(1, "private")
        private = self.server.create_message("Private", "survives restart", 5, "private")
        original_id = self.server.STATE["server_id"]
        self.server.init_db()
        self.server.init_db()
        with self.server.db() as conn:
            self.assertEqual(self.server.message_device_ids(conn, private["id"]), {1})
            self.assertIsNone(self.server.message_device_ids(conn, 1))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(self.server.load_state()["server_id"], original_id)
        self.assertEqual(self._ids(self._sync()), {1, private["id"]})

        # Older deployment wrappers may still invoke this entry point. It must
        # not rewrite IDs that Android cursors and message_devices already use.
        retired = importlib.import_module("id_migration")
        with patch("builtins.print"):
            self.assertEqual(retired.main(), 0)
        with self.server.db() as conn:
            self.assertEqual(self.server.message_device_ids(conn, private["id"]), {1})
            self.assertEqual({x[0] for x in conn.execute("SELECT id FROM messages")}, {1, private["id"]})

    def test_cursor_snapshot_does_not_skip_a_concurrently_committed_message(self):
        with self.server.db() as conn:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
        factory = self.server.db
        injected = []

        class RowsWithConcurrentWrite:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(inner):
                rows = inner.cursor.fetchall()
                if not injected:
                    injected.append(True)
                    injected.append(self.server.create_message("Concurrent", "must not be skipped", 5))
                return rows

        class Connection:
            def __init__(self):
                self.conn = factory()

            def __enter__(self):
                self.conn.__enter__()
                return self

            def __exit__(self, *args):
                try:
                    return self.conn.__exit__(*args)
                finally:
                    self.conn.close()

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, sql, *args):
                cursor = self.conn.execute(sql, *args)
                return RowsWithConcurrentWrite(cursor) if "LEFT JOIN device_reads" in sql else cursor

        with patch.object(self.server, "db", side_effect=Connection):
            first = self.server.get_changes(1, 0)
        self.assertEqual([x["id"] for x in first["items"]], [1])
        second = self.server.get_changes(1, first["cursor"])
        self.assertEqual([x["id"] for x in second["items"]], [injected[1]["id"]])

    def test_authorization_and_mutation_lock_out_concurrent_private_insert(self):
        second = self._register("second", "token-2", "Second phone").get_json()
        authorize = self.server.sync_ids_authorized
        attempted = []

        def authorize_then_attempt_write(conn, device_id, ids):
            allowed = authorize(conn, device_id, ids)
            with sqlite3.connect(self.server.DB_PATH, timeout=0) as writer:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    writer.execute(
                        "INSERT INTO messages(title,message,priority,created_at) VALUES('Private','new',5,'now')"
                    )
            attempted.append(True)
            return allowed

        with patch.object(self.server, "sync_ids_authorized", side_effect=authorize_then_attempt_write):
            response = self._sync(second, read_ids=[2], delete_ids=[2])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempted, [True])
        self._recipient(1, "private")
        created = self.server.create_message("Private", "created after unknown-ID acknowledgement", 5, "private")
        self.assertEqual(created["id"], 2)
        self.assertFalse(created["deleted"])
        self.assertNotIn(2, self._ids(self._sync(second)))

    def test_version_is_consistent_across_app_integration_and_ha_config(self):
        manifest = json.loads((APP_DIR / "integration/yugoha/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], self.server.VERSION)
        self.assertIn(f'version: "{self.server.VERSION}"', (APP_DIR / "config.yaml").read_text(encoding="utf-8"))

    def test_missing_or_wrong_device_credentials_cannot_sync_or_change_token(self):
        for headers in [{}, {"X-YuGoHA-Device": "1", "X-YuGoHA-Secret": "wrong"}]:
            for route in ["/api/sync", "/api/device/token"]:
                with self.subTest(route=route, headers=headers):
                    response = self.client.post(route, headers=headers, json={"delete_ids": [1], "fcm_token": "foreign"})
                    self.assertEqual(response.status_code, 401)
        self.assertFalse(self._sync().get_json()["items"][0]["deleted"])

    def test_two_servers_can_pair_same_phone_token_without_sharing_read_state(self):
        first_server_id = self.server.STATE["server_id"]
        second_state = {
            "pair_code": "654321", "api_key": "second-api",
            "server_id": "b" * 32, "server_name": "Дача",
        }
        with patch.object(self.server, "DB_PATH", self.data_dir / "second.sqlite"), patch.object(
            self.server, "STATE", second_state
        ):
            self.server.init_db()
            phone = self._register("old-client", "old-token", "Same phone").get_json()
            message = self.server.create_message("Second home", "separate database", 5)
            self.assertEqual(message["id"], 1)  # Same numeric ID as the first home.
            result = self._sync(phone, read_ids=[1]).get_json()
            self.assertTrue(result["items"][0]["read"])
            self.assertEqual(result["server_id"], second_state["server_id"])
            self.assertNotEqual(result["server_id"], first_server_id)
        first = self._sync().get_json()
        self.assertEqual(first["server_id"], first_server_id)
        self.assertEqual(first["items"][0]["message"], "Old shared history")
        self.assertFalse(first["items"][0]["read"])


if __name__ == "__main__":
    unittest.main()
