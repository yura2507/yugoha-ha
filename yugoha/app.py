import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, redirect, render_template_string, request
from flask_sock import Sock
import firebase_admin
from firebase_admin import credentials, messaging

from version import VERSION

DATA_DIR = Path(os.environ.get("YUGOHA_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "yugoha.sqlite"
SERVICE_ACCOUNT_PATH = DATA_DIR / "service-account.json"
STATE_PATH = DATA_DIR / "state.json"
OPTIONS_PATH = DATA_DIR / "options.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
sock = Sock(app)

firebase_lock = threading.Lock()
ws_lock = threading.Lock()
ws_clients = {}  # device_id -> set(ws)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def load_options():
    if OPTIONS_PATH.exists():
        try:
            return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state():
    state = None
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if data.get("pair_code") and data.get("api_key"):
                state = data
        except Exception:
            pass

    options = load_options()
    configured_api_key = str(options.get("api_key", "") or "").strip()

    if state is None:
        state = {
            "pair_code": f"{secrets.randbelow(1_000_000):06d}",
            "api_key": configured_api_key or secrets.token_urlsafe(32),
        }

    changed = False
    if not state.get("server_id"):
        state["server_id"] = secrets.token_hex(16)
        changed = True
    if not state.get("server_name"):
        state["server_name"] = str(
            options.get("server_name", "Home Assistant") or "Home Assistant"
        ).strip()[:100]
        changed = True

    if changed or not STATE_PATH.exists():
        save_state(state)
    return state


STATE = load_state()


def ingress_redirect(status=""):
    suffix = f"?status={quote(str(status))}" if status else ""
    return redirect(f"./{suffix}")


def _columns(conn, table):
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO meta(key, value)
            VALUES('sync_version', 0)
        """)

        # Existing 0.1/0.2 installs have a smaller devices/messages schema.
        # Create-if-missing first, then migrate columns in place so /data survives update.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                token TEXT NOT NULL,
                secret TEXT NOT NULL DEFAULT '',
                recipient TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        device_cols = _columns(conn, "devices")
        if "client_id" not in device_cols:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN client_id TEXT NOT NULL DEFAULT ''"
            )
        if "secret" not in device_cols:
            conn.execute(
                "ALTER TABLE devices ADD COLUMN secret TEXT NOT NULL DEFAULT ''"
            )
        if "recipient" not in device_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN recipient TEXT NULL")

        # Backfill identity/security for devices created by v0.1/v0.2.
        for row in conn.execute(
            "SELECT id, client_id, secret FROM devices"
        ).fetchall():
            client_id = str(row["client_id"] or "").strip()
            secret = str(row["secret"] or "").strip()
            if not client_id:
                client_id = f'legacy-{int(row["id"])}'
            if not secret:
                secret = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE devices SET client_id=?, secret=? WHERE id=?",
                (client_id, secret, int(row["id"])),
            )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_client_id ON devices(client_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_token ON devices(token)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                deleted INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                fcm_ok INTEGER NOT NULL DEFAULT 0,
                fcm_error TEXT NOT NULL DEFAULT ''
            )
        """)

        message_cols = _columns(conn, "messages")
        if "updated_at" not in message_cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )
        if "deleted" not in message_cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
            )
        if "version" not in message_cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
            )

        # Old rows become valid initial sync history.
        conn.execute(
            "UPDATE messages SET updated_at=created_at WHERE updated_at=''"
        )
        conn.execute(
            "UPDATE messages SET version=id WHERE version=0"
        )

        max_version = int(
            conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM messages"
            ).fetchone()["v"]
        )
        conn.execute(
            "UPDATE meta SET value=MAX(value, ?) WHERE key='sync_version'",
            (max_version,),
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_version ON messages(version)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_reads (
                device_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(device_id, message_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_device_reads_version ON device_reads(device_id, version)"
        )


init_db()


def next_version(conn):
    conn.execute(
        "UPDATE meta SET value=value+1 WHERE key='sync_version'"
    )
    return int(
        conn.execute(
            "SELECT value FROM meta WHERE key='sync_version'"
        ).fetchone()["value"]
    )


def current_version(conn):
    return int(
        conn.execute(
            "SELECT value FROM meta WHERE key='sync_version'"
        ).fetchone()["value"]
    )


def firebase_project_id():
    if not SERVICE_ACCOUNT_PATH.exists():
        return ""
    try:
        data = json.loads(
            SERVICE_ACCOUNT_PATH.read_text(encoding="utf-8")
        )
        return str(data.get("project_id", ""))
    except Exception:
        return ""


def ensure_firebase():
    with firebase_lock:
        if firebase_admin._apps:
            return True
        if not SERVICE_ACCOUNT_PATH.exists():
            return False

        cred = credentials.Certificate(str(SERVICE_ACCOUNT_PATH))
        firebase_admin.initialize_app(cred)
        return True


def api_authorized():
    supplied = (
        request.headers.get("X-YuGoHA-Key", "")
        or request.args.get("key", "")
    )
    return secrets.compare_digest(
        str(supplied),
        str(STATE["api_key"]),
    )


def get_device_from_request():
    raw_id = request.headers.get("X-YuGoHA-Device", "")
    supplied_secret = request.headers.get("X-YuGoHA-Secret", "")

    try:
        device_id = int(raw_id)
    except Exception:
        return None

    with db() as conn:
        row = conn.execute(
            """
            SELECT id, client_id, name, token, secret
            FROM devices
            WHERE id=?
            """,
            (device_id,),
        ).fetchone()

    if row is None:
        return None

    if not secrets.compare_digest(
        str(row["secret"]),
        str(supplied_secret),
    ):
        return None

    return row


def message_payload(row):
    return {
        "id": int(row["id"]),
        "title": str(row["title"] or ""),
        "message": str(row["message"] or ""),
        "priority": int(row["priority"] or 0),
        "date": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "deleted": bool(row["deleted"]),
        "version": int(row["version"] or 0),
    }


def recipient_value(value):
    value = str(value or "").strip()
    return value[:100] or None


def server_fields():
    return {
        "server_id": str(STATE["server_id"]),
        "server_name": str(STATE["server_name"]),
    }


def send_fcm_new(row, recipient=None):
    payload = message_payload(row)
    recipient = recipient_value(recipient)

    with db() as conn:
        if recipient is None:
            devices = conn.execute(
                "SELECT id, name, token FROM devices ORDER BY id"
            ).fetchall()
        else:
            devices = conn.execute(
                "SELECT id, name, token FROM devices WHERE recipient=? ORDER BY id",
                (recipient,),
            ).fetchall()

    if not devices:
        if recipient is None:
            raise RuntimeError("Нет зарегистрированных устройств")
        return 0, []

    if not ensure_firebase():
        raise RuntimeError("service-account.json не загружен")

    ok = 0
    errors = []

    for device in devices:
        try:
            msg = messaging.Message(
                token=device["token"],
                data={
                    "type": "yugoha_message",
                    "id": str(payload["id"]),
                    "title": payload["title"],
                    "message": payload["message"],
                    "date": payload["date"],
                    "updated_at": payload["updated_at"],
                    "priority": str(payload["priority"]),
                    "version": str(payload["version"]),
                    **server_fields(),
                },
                android=messaging.AndroidConfig(
                    priority="high",
                    ttl=3600,
                ),
            )
            messaging.send(msg)
            ok += 1
        except Exception as exc:
            errors.append(f'{device["name"]}: {exc}')

    return ok, errors


def send_fcm_delete(message_id, version):
    if not ensure_firebase():
        return

    with db() as conn:
        devices = conn.execute(
            "SELECT name, token FROM devices ORDER BY id"
        ).fetchall()

    for device in devices:
        try:
            msg = messaging.Message(
                token=device["token"],
                data={
                    "type": "yugoha_delete",
                    "id": str(message_id),
                    "version": str(version),
                },
                android=messaging.AndroidConfig(
                    priority="high",
                    ttl=3600,
                ),
            )
            messaging.send(msg)
        except Exception:
            pass


def ws_broadcast(event, device_ids=None):
    raw = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    dead = []

    with ws_lock:
        for device_id, sockets in list(ws_clients.items()):
            if device_ids is not None and device_id not in device_ids:
                continue
            for ws in list(sockets):
                try:
                    ws.send(raw)
                except Exception:
                    dead.append((device_id, ws))

        for device_id, ws in dead:
            sockets = ws_clients.get(device_id)
            if sockets is not None:
                sockets.discard(ws)
                if not sockets:
                    ws_clients.pop(device_id, None)


def create_message(title, body, priority, recipient=None):
    now = utc_now()

    with db() as conn:
        version = next_version(conn)
        cur = conn.execute(
            """
            INSERT INTO messages(
                title, message, priority,
                created_at, updated_at,
                deleted, version
            )
            VALUES(?,?,?,?,?,0,?)
            """,
            (
                title,
                body,
                priority,
                now,
                now,
                version,
            ),
        )
        message_id = int(cur.lastrowid)

        row = conn.execute(
            "SELECT * FROM messages WHERE id=?",
            (message_id,),
        ).fetchone()

    recipient = recipient_value(recipient)
    target_ids = None
    if recipient is not None:
        with db() as conn:
            target_ids = {
                int(item["id"])
                for item in conn.execute(
                    "SELECT id FROM devices WHERE recipient=?", (recipient,)
                ).fetchall()
            }

    ws_broadcast({
        "type": "message",
        "id": message_id,
        "version": version,
        **server_fields(),
    }, target_ids)

    return row


def delete_messages(ids):
    changed = []

    with db() as conn:
        for message_id in sorted(set(int(x) for x in ids)):
            row = conn.execute(
                "SELECT id, deleted FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()

            if row is None or int(row["deleted"]) == 1:
                continue

            version = next_version(conn)
            now = utc_now()

            conn.execute(
                """
                UPDATE messages
                SET deleted=1, updated_at=?, version=?
                WHERE id=?
                """,
                (now, version, message_id),
            )

            changed.append((message_id, version))

    for message_id, version in changed:
        ws_broadcast({
            "type": "delete",
            "id": message_id,
            "version": version,
        })
        send_fcm_delete(message_id, version)

    return changed


def apply_reads(device_id, ids):
    if not ids:
        return 0

    count = 0
    with db() as conn:
        for message_id in sorted(set(int(x) for x in ids)):
            exists = conn.execute(
                "SELECT id FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()

            if exists is None:
                continue

            current = conn.execute(
                """
                SELECT is_read
                FROM device_reads
                WHERE device_id=? AND message_id=?
                """,
                (device_id, message_id),
            ).fetchone()

            if current is not None and int(current["is_read"]) == 1:
                continue

            version = next_version(conn)
            conn.execute(
                """
                INSERT INTO device_reads(
                    device_id, message_id,
                    is_read, updated_at, version
                )
                VALUES(?,?,1,?,?)
                ON CONFLICT(device_id, message_id)
                DO UPDATE SET
                    is_read=1,
                    updated_at=excluded.updated_at,
                    version=excluded.version
                """,
                (
                    device_id,
                    message_id,
                    utc_now(),
                    version,
                ),
            )
            count += 1

    return count


def get_changes(device_id, after_version, limit=500):
    limit = max(1, min(int(limit), 1000))

    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.title,
                m.message,
                m.priority,
                m.created_at,
                m.updated_at,
                m.deleted,
                m.version AS message_version,
                COALESCE(r.is_read, 0) AS is_read,
                COALESCE(r.version, 0) AS read_version
            FROM messages m
            LEFT JOIN device_reads r
              ON r.device_id=?
             AND r.message_id=m.id
            WHERE
                m.version > ?
                OR COALESCE(r.version, 0) > ?
            ORDER BY
                MAX(m.version, COALESCE(r.version, 0)) ASC,
                m.id ASC
            LIMIT ?
            """,
            (
                device_id,
                after_version,
                after_version,
                limit + 1,
            ),
        ).fetchall()

        global_cursor = current_version(conn)

    has_more = len(rows) > limit
    rows = rows[:limit]

    items = []
    max_seen = after_version

    for row in rows:
        version = max(
            int(row["message_version"] or 0),
            int(row["read_version"] or 0),
        )
        max_seen = max(max_seen, version)

        items.append({
            "id": int(row["id"]),
            "title": str(row["title"] or ""),
            "message": str(row["message"] or ""),
            "priority": int(row["priority"] or 0),
            "date": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "deleted": bool(row["deleted"]),
            "read": bool(row["is_read"]),
            "version": version,
        })

    cursor = max_seen if has_more else global_cursor

    return {
        "items": items,
        "cursor": cursor,
        "has_more": has_more,
    }


INDEX_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yuGoHA Server</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f4f6f8;color:#1f2937}
.wrap{max-width:920px;margin:auto;padding:18px}
.card{background:white;border-radius:14px;padding:18px;margin:12px 0;box-shadow:0 1px 5px #0001}
h1{margin:0 0 6px}.muted{color:#667085}.ok{color:#16803d}.bad{color:#b42318}.warn{color:#b54708}
code,.mono{font-family:ui-monospace,monospace;word-break:break-all}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cfd5dd;border-radius:9px;margin:5px 0 10px}
button{padding:10px 14px;border:0;border-radius:9px;background:#1677ff;color:white;font-weight:600;cursor:pointer}
button.secondary{background:#667085}button.danger{background:#b42318}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1}
.small{font-size:13px}.pill{display:inline-block;padding:3px 8px;border-radius:20px;background:#eef2f6;font-size:12px}
</style>
</head>
<body><div class="wrap">
<h1>yuGoHA Server <span class="pill">v{{app_version}}</span></h1>
<div class="muted">HTTP/WebSocket локально + FCM для доставки во сне и за CGNAT</div>

{% if status %}<div class="card"><b>{{status}}</b></div>{% endif %}

<div class="card">
<h3>1. Firebase</h3>
{% if firebase_project %}
<div class="ok">✓ Project ID: <b>{{firebase_project}}</b></div>
{% else %}
<div class="bad">service-account.json не загружен</div>
{% endif %}
<form action="./upload_service_account" method="post" enctype="multipart/form-data">
<input type="file" name="file" accept=".json,application/json" required>
<button type="submit">ЗАГРУЗИТЬ SERVICE-ACCOUNT.JSON</button>
</form>
<div class="small muted">Файл хранится только локально в /data App.</div>
</div>

<div class="card">
<h3>2. Подключение телефона</h3>
<div>Код сопряжения:</div>
<div style="font-size:38px;font-weight:750;letter-spacing:5px">{{pair_code}}</div>
<form action="./new_pair_code" method="post">
<button type="submit">СОЗДАТЬ НОВЫЙ КОД</button>
</form>

<p>Устройств: <b>{{devices|length}}</b></p>
{% for d in devices %}
<div style="border-top:1px solid #e5e7eb;padding-top:10px;margin-top:10px">
<form action="./rename_device" method="post">
<input type="hidden" name="device_id" value="{{d["id"]}}">
<div class="row">
<input name="name" value="{{d["name"]}}" maxlength="100" required>
<button type="submit" class="secondary">ПЕРЕИМЕНОВАТЬ</button>
</div>
</form>
<form action="./set_recipient" method="post">
<input type="hidden" name="device_id" value="{{d["id"]}}">
<div class="row">
<input name="recipient" value="{{d["recipient"] or ''}}" maxlength="100" placeholder="Получатель, например yura">
<button type="submit" class="secondary">СОХРАНИТЬ ПОЛУЧАТЕЛЯ</button>
</div>
</form>
<div class="small muted">Пустое поле означает получение общих сообщений.</div>
<div class="small muted">Последняя регистрация: {{d["updated_at"]}}</div>
<form action="./test_device" method="post">
<input type="hidden" name="device_id" value="{{d["id"]}}">
<input name="message" value="1.info. Тестовое сообщение FCM" required>
<button type="submit">ТЕСТ ЭТОГО УСТРОЙСТВА</button>
</form>
<form action="./delete_device" method="post" onsubmit="return confirm('Удалить устройство?')">
<input type="hidden" name="device_id" value="{{d["id"]}}">
<button type="submit" class="danger">УДАЛИТЬ УСТРОЙСТВО</button>
</form>
</div>
{% endfor %}
</div>

<div class="card">
<h3>3. Home Assistant API</h3>
<div class="mono">{{api_key}}</div>
<form action="./new_api_key" method="post">
<button type="submit">СОЗДАТЬ НОВЫЙ API KEY</button>
</form>
<div class="small muted">
Интеграция yuGoHA использует этот API key локально.
Порт наружу открывать не нужно.
</div>
</div>

<div class="card">
<h3>4. Диагностика</h3>
<div>Firebase: <b>{{firebase_project or 'не настроен'}}</b></div>
<div>Устройств: <b>{{devices|length}}</b></div>
<div>Сообщений: <b>{{message_count}}</b></div>
<div>Удалённых: <b>{{deleted_count}}</b></div>
<div>Sync version: <b>{{sync_version}}</b></div>
<div>WebSocket клиентов: <b>{{ws_count}}</b></div>
<div>Последняя отправка: <b>{{last_summary}}</b></div>
{% if last_error %}<div class="bad small">{{last_error}}</div>{% endif %}
</div>

<div class="card">
<h3>5. Общий тест FCM</h3>
<form action="./test" method="post">
<input name="message" value="1.info. Тест yuGoHA Server" required>
<div class="row">
<input name="priority" type="number" min="0" max="10" value="5">
<button type="submit">ОТПРАВИТЬ ВСЕМ</button>
</div>
</form>
</div>

<div class="card">
<h3>О проекте</h3>
<div>Автор: <b>yura2507</b></div>
<div><a href="https://dzen.ru/yura2507" target="_blank" rel="noopener">Дзен автора: dzen.ru/yura2507</a></div>
<div><a href="https://github.com/yura2507/yugoha-ha" target="_blank" rel="noopener">GitHub: yura2507/yugoha-ha</a></div>
</div>
</div></body></html>
"""


@app.get("/")
def index():
    status = request.args.get("status", "")

    with db() as conn:
        devices = conn.execute(
            "SELECT id, name, recipient, updated_at FROM devices ORDER BY id"
        ).fetchall()
        message_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages"
        ).fetchone()["c"]
        deleted_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE deleted=1"
        ).fetchone()["c"]
        sync_version = current_version(conn)
        last = conn.execute(
            """
            SELECT created_at, fcm_ok, fcm_error
            FROM messages
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if last is None:
        last_summary = "ещё не было отправок"
        last_error = ""
    else:
        last_summary = (
            f'{last["created_at"]} · доставлено FCM: '
            f'{int(last["fcm_ok"] or 0)}'
        )
        last_error = str(last["fcm_error"] or "")

    with ws_lock:
        ws_count = sum(len(x) for x in ws_clients.values())

    return render_template_string(
        INDEX_HTML,
        app_version=VERSION,
        status=status,
        firebase_project=firebase_project_id(),
        pair_code=STATE["pair_code"],
        api_key=STATE["api_key"],
        devices=devices,
        message_count=message_count,
        deleted_count=deleted_count,
        sync_version=sync_version,
        ws_count=ws_count,
        last_summary=last_summary,
        last_error=last_error,
    )


@app.post("/upload_service_account")
def upload_service_account():
    f = request.files.get("file")
    if not f:
        return ingress_redirect("Файл не выбран")

    try:
        data = json.load(f.stream)
        if data.get("type") != "service_account":
            raise ValueError("Это не service_account JSON")
        if not data.get("project_id") or not data.get("private_key"):
            raise ValueError("Нет project_id/private_key")

        SERVICE_ACCOUNT_PATH.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        status = (
            f'Firebase сохранён: {data["project_id"]}. '
            'Если проект менялся — перезапусти App.'
        )
    except Exception as exc:
        status = f"Ошибка Firebase JSON: {exc}"

    return ingress_redirect(status)


@app.post("/new_pair_code")
def new_pair_code():
    STATE["pair_code"] = f"{secrets.randbelow(1_000_000):06d}"
    save_state(STATE)
    return ingress_redirect("Новый код сопряжения создан")


@app.post("/new_api_key")
def new_api_key():
    STATE["api_key"] = secrets.token_urlsafe(32)
    save_state(STATE)
    return ingress_redirect("Новый API key создан")


@app.post("/rename_device")
def rename_device():
    try:
        device_id = int(request.form.get("device_id", "0"))
    except Exception:
        device_id = 0

    name = str(request.form.get("name", "")).strip()[:100]

    if device_id <= 0 or not name:
        return ingress_redirect("Некорректные данные устройства")

    with db() as conn:
        cur = conn.execute(
            "UPDATE devices SET name=? WHERE id=?",
            (name, device_id),
        )

    return ingress_redirect(
        "Устройство переименовано"
        if cur.rowcount
        else "Устройство не найдено"
    )


@app.post("/set_recipient")
def set_recipient():
    try:
        device_id = int(request.form.get("device_id", "0"))
    except Exception:
        device_id = 0

    recipient = recipient_value(request.form.get("recipient"))
    if device_id <= 0:
        return ingress_redirect("Некорректные данные устройства")

    with db() as conn:
        cur = conn.execute(
            "UPDATE devices SET recipient=? WHERE id=?",
            (recipient, device_id),
        )

    return ingress_redirect(
        "Получатель сохранён"
        if cur.rowcount
        else "Устройство не найдено"
    )


@app.post("/delete_device")
def delete_device():
    try:
        device_id = int(request.form.get("device_id", "0"))
    except Exception:
        device_id = 0

    with db() as conn:
        cur = conn.execute(
            "DELETE FROM devices WHERE id=?",
            (device_id,),
        )
        conn.execute(
            "DELETE FROM device_reads WHERE device_id=?",
            (device_id,),
        )

    return ingress_redirect(
        "Устройство удалено"
        if cur.rowcount
        else "Устройство не найдено"
    )


@app.post("/api/register")
def register_device():
    payload = request.get_json(silent=True) or {}

    pair_code = str(payload.get("pair_code", "")).strip()
    token = str(payload.get("fcm_token", "")).strip()
    name = str(payload.get("name", "Android")).strip()[:100] or "Android"
    client_id = str(payload.get("client_id", "")).strip()

    if not secrets.compare_digest(
        pair_code,
        STATE["pair_code"],
    ):
        return jsonify(
            ok=False,
            error="Неверный код сопряжения",
        ), 403

    if not token:
        return jsonify(
            ok=False,
            error="FCM token пуст",
        ), 400

    if not client_id:
        return jsonify(
            ok=False,
            error="client_id пуст",
        ), 400

    now = utc_now()

    with db() as conn:
        existing = conn.execute(
            "SELECT id, secret FROM devices WHERE client_id=?",
            (client_id,),
        ).fetchone()

        # Upgrade path: v0.1/v0.2 knew only the FCM token, not client_id.
        # Reuse that row instead of creating a duplicate with the same token.
        if existing is None:
            existing = conn.execute(
                "SELECT id, secret FROM devices WHERE token=?",
                (token,),
            ).fetchone()

        if existing is None:
            secret = secrets.token_urlsafe(32)
            cur = conn.execute(
                """
                INSERT INTO devices(
                    client_id, name, token, secret,
                    created_at, updated_at
                )
                VALUES(?,?,?,?,?,?)
                """,
                (
                    client_id,
                    name,
                    token,
                    secret,
                    now,
                    now,
                ),
            )
            device_id = int(cur.lastrowid)
        else:
            device_id = int(existing["id"])
            secret = str(existing["secret"] or "").strip() or secrets.token_urlsafe(32)
            conn.execute(
                """
                UPDATE devices
                SET client_id=?, name=?, token=?, secret=?, updated_at=?
                WHERE id=?
                """,
                (
                    client_id,
                    name,
                    token,
                    secret,
                    now,
                    device_id,
                ),
            )

    return jsonify(
        ok=True,
        device_id=device_id,
        device_secret=secret,
        firebase_project=firebase_project_id(),
        **server_fields(),
    )


@app.post("/api/device/token")
def update_device_token():
    device = get_device_from_request()
    if device is None:
        return jsonify(ok=False, error="Unauthorized device"), 401

    payload = request.get_json(silent=True) or {}
    token = str(payload.get("fcm_token", "")).strip()
    if not token:
        return jsonify(ok=False, error="FCM token пуст"), 400

    with db() as conn:
        conn.execute(
            "UPDATE devices SET token=?, updated_at=? WHERE id=?",
            (token, utc_now(), int(device["id"])),
        )

    return jsonify(ok=True)


@app.post("/api/sync")
def sync():
    device = get_device_from_request()
    if device is None:
        return jsonify(
            ok=False,
            error="Unauthorized device",
        ), 401

    payload = request.get_json(silent=True) or {}

    try:
        cursor = max(0, int(payload.get("cursor", 0)))
    except Exception:
        cursor = 0

    read_ids = payload.get("read_ids") or []
    delete_ids = payload.get("delete_ids") or []

    try:
        read_ids = [int(x) for x in read_ids]
        delete_ids = [int(x) for x in delete_ids]
    except Exception:
        return jsonify(
            ok=False,
            error="Некорректные id",
        ), 400

    apply_reads(int(device["id"]), read_ids)
    deleted = delete_messages(delete_ids)

    result = get_changes(
        int(device["id"]),
        cursor,
        limit=500,
    )

    return jsonify(
        ok=True,
        ack_read=read_ids,
        ack_delete=delete_ids,
        deleted_now=[x[0] for x in deleted],
        **result,
    )


@app.get("/api/health")
def health():
    with db() as conn:
        devices = conn.execute(
            "SELECT COUNT(*) AS c FROM devices"
        ).fetchone()["c"]
        version = current_version(conn)

    return jsonify(
        ok=True,
        version=VERSION,
        firebase_project=firebase_project_id(),
        firebase_ready=SERVICE_ACCOUNT_PATH.exists(),
        devices=devices,
        sync_version=version,
        **server_fields(),
    )


@app.post("/api/message")
def api_message():
    if not api_authorized():
        return jsonify(
            ok=False,
            error="Unauthorized",
        ), 401

    payload = request.get_json(silent=True) or {}
    body = str(payload.get("message", "")).strip()
    recipient = recipient_value(payload.get("recipient"))
    title = str(
        payload.get(
            "title",
            "Home Assistant",
        )
    ).strip()

    try:
        priority = int(payload.get("priority", 5))
    except Exception:
        priority = 5

    priority = max(0, min(priority, 10))

    if not body:
        return jsonify(
            ok=False,
            error="message is required",
        ), 400

    row = create_message(
        title,
        body,
        priority,
        recipient,
    )

    message_id = int(row["id"])

    try:
        ok_count, errors = send_fcm_new(row, recipient)

        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET fcm_ok=?, fcm_error=?
                WHERE id=?
                """,
                (
                    ok_count,
                    "\n".join(errors),
                    message_id,
                ),
            )

        return jsonify(
            ok=True,
            id=message_id,
            version=int(row["version"]),
            delivered=ok_count,
            recipient=recipient,
            errors=errors,
        )
    except Exception as exc:
        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET fcm_error=?
                WHERE id=?
                """,
                (
                    str(exc),
                    message_id,
                ),
            )

        return jsonify(
            ok=False,
            id=message_id,
            error=str(exc),
        ), 500


@app.post("/test_device")
def test_device():
    try:
        device_id = int(request.form.get("device_id", "0"))
    except Exception:
        device_id = 0

    body = str(
        request.form.get(
            "message",
            "1.info. Тестовое сообщение FCM",
        )
    ).strip()

    with db() as conn:
        device = conn.execute(
            """
            SELECT id, name, token
            FROM devices
            WHERE id=?
            """,
            (device_id,),
        ).fetchone()

    if device is None:
        return ingress_redirect("Устройство не найдено")

    row = create_message(
        "yuGoHA Server",
        body,
        5,
    )

    try:
        if not ensure_firebase():
            raise RuntimeError(
                "service-account.json не загружен"
            )

        msg = messaging.Message(
            token=device["token"],
            data={
                "type": "yugoha_message",
                "id": str(row["id"]),
                "title": str(row["title"]),
                "message": str(row["message"]),
                "date": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "priority": str(row["priority"]),
                "version": str(row["version"]),
            },
            android=messaging.AndroidConfig(
                priority="high",
                ttl=3600,
            ),
        )

        response = messaging.send(msg)

        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET fcm_ok=1, fcm_error=''
                WHERE id=?
                """,
                (row["id"],),
            )

        status = (
            f'FCM отправлен на {device["name"]}: '
            f'{response}'
        )
    except Exception as exc:
        status = f"Ошибка FCM: {exc}"

    return ingress_redirect(status)


@app.post("/test")
def test_all():
    body = str(
        request.form.get(
            "message",
            "1.info. Тест yuGoHA Server",
        )
    ).strip()

    try:
        priority = int(
            request.form.get(
                "priority",
                "5",
            )
        )
    except Exception:
        priority = 5

    row = create_message(
        "yuGoHA Server",
        body,
        max(0, min(priority, 10)),
    )

    try:
        ok_count, errors = send_fcm_new(row)

        with db() as conn:
            conn.execute(
                """
                UPDATE messages
                SET fcm_ok=?, fcm_error=?
                WHERE id=?
                """,
                (
                    ok_count,
                    "\n".join(errors),
                    row["id"],
                ),
            )

        status = f"FCM отправлен: {ok_count} устройств"
        if errors:
            status += f"; ошибок: {len(errors)}"
    except Exception as exc:
        status = f"Ошибка FCM: {exc}"

    return ingress_redirect(status)


@sock.route("/ws")
def websocket(ws):
    try:
        device_id = int(
            request.args.get(
                "device_id",
                "0",
            )
        )
    except Exception:
        return

    supplied_secret = request.args.get("secret", "")

    with db() as conn:
        row = conn.execute(
            "SELECT id, secret FROM devices WHERE id=?",
            (device_id,),
        ).fetchone()

    if row is None:
        return

    if not secrets.compare_digest(
        str(row["secret"]),
        str(supplied_secret),
    ):
        return

    with ws_lock:
        ws_clients.setdefault(
            device_id,
            set(),
        ).add(ws)

    try:
        ws.send(
            json.dumps({
                "type": "hello",
                "server": "yuGoHA",
                "version": VERSION,
                **server_fields(),
            })
        )

        while True:
            raw = ws.receive()
            if raw is None:
                break

            try:
                data = json.loads(raw)
            except Exception:
                continue

            if data.get("type") == "ping":
                ws.send('{"type":"pong"}')
            elif data.get("type") == "sync":
                ws.send('{"type":"sync_hint"}')
    finally:
        with ws_lock:
            sockets = ws_clients.get(device_id)
            if sockets is not None:
                sockets.discard(ws)
                if not sockets:
                    ws_clients.pop(
                        device_id,
                        None,
                    )
