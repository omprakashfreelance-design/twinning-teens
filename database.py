import os
import sqlite3
from datetime import datetime, timedelta, timezone


DATABASE_PATH = os.environ.get("DATABASE_PATH", "kid_reward_system.db")
DEVICE_OFFLINE_AFTER_SECONDS = int(os.environ.get("DEVICE_OFFLINE_AFTER_SECONDS", "45"))


DEFAULT_SETTINGS = {
    "wifi_ssid": "",
    "wifi_password": "",
    "server_url": os.environ.get("SERVER_URL", ""),
    "noise_threshold": "6000",
    "record_sec": "5",
    "monitoring_enabled": "1",
}


DEFAULT_TASKS = [
    ("Finish Homework", "Pending"),
    ("Clean Room", "Completed"),
]


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                device_type TEXT NOT NULL,
                current_ip TEXT,
                status TEXT NOT NULL DEFAULT 'offline',
                last_seen TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('Pending', 'Completed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                command TEXT NOT NULL,
                payload TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                FOREIGN KEY(device_id) REFERENCES devices(device_id)
            )
            """
        )
        _seed_defaults(conn)


def _seed_defaults(conn):
    now = utc_now_iso()
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now),
        )

    task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    if task_count == 0:
        conn.executemany(
            """
            INSERT INTO tasks (name, status, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            [(name, status, now, now) for name, status in DEFAULT_TASKS],
        )


def row_to_dict(row):
    return dict(row) if row else None


def get_settings():
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    merged = {**DEFAULT_SETTINGS, **settings}
    return {
        "wifi_ssid": merged["wifi_ssid"],
        "wifi_password": merged["wifi_password"],
        "server_url": merged["server_url"],
        "noise_threshold": int(merged["noise_threshold"] or 0),
        "record_sec": int(merged["record_sec"] or 5),
        "monitoring_enabled": merged["monitoring_enabled"] in ("1", "true", "True", "on"),
    }


def update_settings(values):
    now = utc_now_iso()
    normalized = {
        "wifi_ssid": values.get("wifi_ssid", ""),
        "wifi_password": values.get("wifi_password", ""),
        "server_url": values.get("server_url", ""),
        "noise_threshold": str(int(values.get("noise_threshold") or 0)),
        "record_sec": str(int(values.get("record_sec") or 5)),
        "monitoring_enabled": "1" if values.get("monitoring_enabled") else "0",
    }
    with get_connection() as conn:
        for key, value in normalized.items():
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )


def upsert_device(device_id, device_type, current_ip=None, status="online"):
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO devices (
                device_id, device_type, current_ip, status, last_seen, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                device_type = excluded.device_type,
                current_ip = COALESCE(excluded.current_ip, devices.current_ip),
                status = excluded.status,
                last_seen = excluded.last_seen,
                updated_at = excluded.updated_at
            """,
            (device_id, device_type, current_ip, status, now, now, now),
        )


def get_devices():
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DEVICE_OFFLINE_AFTER_SECONDS)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE devices
            SET status = 'offline'
            WHERE last_seen IS NULL OR last_seen < ?
            """,
            (cutoff_iso,),
        )
        rows = conn.execute(
            """
            SELECT device_id, device_type, current_ip, status, last_seen
            FROM devices
            ORDER BY device_type, device_id
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_device(device_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    return row_to_dict(row)


def find_device_by_type(device_type):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM devices
            WHERE lower(device_type) = lower(?)
            ORDER BY
                CASE status WHEN 'online' THEN 0 ELSE 1 END,
                last_seen DESC
            LIMIT 1
            """,
            (device_type,),
        ).fetchone()
    return row_to_dict(row)


def get_tasks():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, status FROM tasks ORDER BY id DESC"
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def add_task(name):
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tasks (name, status, created_at, updated_at) VALUES (?, 'Pending', ?, ?)",
            (name, now, now),
        )


def complete_task(task_id):
    with get_connection() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'Completed', updated_at = ? WHERE id = ?",
            (utc_now_iso(), task_id),
        )


def enqueue_command(device_id, command, payload=None):
    now = utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO device_commands (device_id, command, payload, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (device_id, command, payload, now),
        )


def pop_next_command(device_id):
    now = utc_now_iso()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, command, payload
            FROM device_commands
            WHERE device_id = ? AND status = 'pending'
            ORDER BY id
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE device_commands
            SET status = 'delivered', delivered_at = ?
            WHERE id = ?
            """,
            (now, row["id"]),
        )
    return row_to_dict(row)
