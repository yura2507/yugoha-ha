#!/usr/bin/env python3
import sqlite3
import time
from pathlib import Path

DB_PATH = Path("/data/yugoha.sqlite")
MIGRATION_KEY = "global_message_ids_v1"


def log(message: str) -> None:
    print(f"[yuGoHA ids] {message}", flush=True)


def main() -> int:
    if not DB_PATH.exists():
        log("database not created yet; nothing to migrate")
        return 0

    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS yugoha_migrations (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

        if conn.execute(
            "SELECT 1 FROM yugoha_migrations WHERE key=?",
            (MIGRATION_KEY,),
        ).fetchone():
            return 0

        has_messages = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()

        if not has_messages:
            conn.execute(
                "INSERT OR REPLACE INTO yugoha_migrations(key,value) VALUES(?,?)",
                (MIGRATION_KEY, "waiting_for_messages_table"),
            )
            conn.commit()
            return 0

        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages"
        ).fetchone()
        max_id = int(row[0] or 0)

        # New server instance gets its own time-based 64-bit namespace.
        # Microseconds since Unix epoch are ~1.8e15 and safely fit Android Long/SQLite INTEGER.
        base = int(time.time_ns() // 1000)

        if max_id > 0 and max_id < 1_000_000_000_000:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE messages SET id = id + ?",
                (base,),
            )

            has_reads = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_reads'"
            ).fetchone()
            if has_reads:
                conn.execute(
                    "UPDATE device_reads SET message_id = message_id + ?",
                    (base,),
                )

            new_max = int(
                conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0] or 0
            )

            # Keep AUTOINCREMENT above the migrated range.
            conn.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES('messages',?)",
                (new_max,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO yugoha_migrations(key,value) VALUES(?,?)",
                (MIGRATION_KEY, str(base)),
            )
            conn.commit()
            log(f"migrated legacy message ids by +{base}; new max id={new_max}")
        else:
            # Empty DB: seed sqlite_sequence so the first new message also gets a globally unique id.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES('messages',?)",
                (base,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO yugoha_migrations(key,value) VALUES(?,?)",
                (MIGRATION_KEY, str(base)),
            )
            conn.commit()
            log(f"seeded global message id namespace at {base}")

        return 0
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        log(f"migration warning: {exc}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
