"""Install and activate the Aniki Pipe in Open WebUI's SQLite database.

Open WebUI creates and migrates its database first. A one-shot Compose service
then runs this script against the shared data volume. Repeated runs are safe:
source and metadata are updated while existing valve customizations are kept.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL_ID = "aniki"
MODEL_NAME = "Aniki"
DEFAULT_DB_PATH = "/app/backend/data/webui.db"
DEFAULT_SOURCE_PATH = "/opt/aniki/openwebui_aniki_pipe.py"

MANIFEST = {
    "title": MODEL_NAME,
    "author": "Local",
    "description": (
        "上傳 CMM PDF 或圖片並送出後，自動辨識並寫入本地 PostgreSQL。"
    ),
    "required_open_webui_version": "0.9.0",
    "requirements": "requests",
    "version": "1.3.0",
    "license": "MIT",
}


def table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set[str]:
    rows = connection.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall()
    return {str(row[1]) for row in rows}


def table_column_types(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, str]:
    rows = connection.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall()
    return {str(row[1]): str(row[2]).upper() for row in rows}


def timestamp_for_column(column_type: str) -> int | str:
    """Match the column's declared SQL affinity.

    Open WebUI stores some timestamp columns as epoch integers
    (INTEGER/BIGINT) and others as SQLite DATETIME text
    (e.g. "2026-08-18 12:42:15"). Writing the wrong shape corrupts
    the row: SQLAlchemy's DateTime processor calls
    datetime.fromisoformat() on read, which raises TypeError on an
    int and crashes Open WebUI on every subsequent startup.
    """
    if "INT" in column_type:
        return int(time.time())
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def find_owner_id(
    connection: sqlite3.Connection,
) -> str | None:
    columns = table_columns(connection, "user")
    if not columns:
        return None

    order_parts: list[str] = []
    if "role" in columns:
        order_parts.append(
            "CASE WHEN role = 'admin' THEN 0 ELSE 1 END"
        )
    if "created_at" in columns:
        order_parts.append("created_at")

    order_sql = (
        " ORDER BY " + ", ".join(order_parts)
        if order_parts
        else ""
    )
    row = connection.execute(
        f'SELECT id FROM "user"{order_sql} LIMIT 1'
    ).fetchone()
    return str(row[0]) if row else None


def install_function(
    connection: sqlite3.Connection,
    source: str,
) -> None:
    columns = table_columns(connection, "function")
    required = {
        "id",
        "name",
        "type",
        "content",
        "meta",
        "is_active",
    }
    if not required.issubset(columns):
        missing = ", ".join(sorted(required - columns))
        raise RuntimeError(
            f"Open WebUI function schema 尚未就緒；缺少欄位：{missing}"
        )

    now = int(time.time())
    owner_id = find_owner_id(connection)
    existing = connection.execute(
        'SELECT id FROM "function" WHERE id = ?',
        (MODEL_ID,),
    ).fetchone()
    meta_json = json.dumps(
        {
            "description": MANIFEST["description"],
            "manifest": MANIFEST,
        },
        ensure_ascii=False,
    )

    if existing:
        assignments = [
            "name = ?",
            "type = ?",
            "content = ?",
            "meta = ?",
            "is_active = ?",
        ]
        values: list[Any] = [
            MODEL_NAME,
            "pipe",
            source,
            meta_json,
            1,
        ]
        if "is_global" in columns:
            assignments.append("is_global = ?")
            values.append(0)
        if "updated_at" in columns:
            assignments.append("updated_at = ?")
            values.append(now)
        if "user_id" in columns and owner_id:
            assignments.append(
                "user_id = COALESCE(user_id, ?)"
            )
            values.append(owner_id)

        values.append(MODEL_ID)
        connection.execute(
            'UPDATE "function" SET '
            + ", ".join(assignments)
            + " WHERE id = ?",
            values,
        )
        return

    row: dict[str, Any] = {
        "id": MODEL_ID,
        "name": MODEL_NAME,
        "type": "pipe",
        "content": source,
        "meta": meta_json,
        "is_active": 1,
    }
    optional_values = {
        "user_id": owner_id,
        "valves": None,
        "is_global": 0,
        "updated_at": now,
        "created_at": now,
    }
    row.update(
        {
            key: value
            for key, value in optional_values.items()
            if key in columns
        }
    )

    names = list(row)
    quoted_names = ", ".join(
        f'"{name}"' for name in names
    )
    placeholders = ", ".join("?" for _ in names)
    connection.execute(
        f'INSERT INTO "function" ({quoted_names}) '
        f"VALUES ({placeholders})",
        [row[name] for name in names],
    )


def install_default_model(
    connection: sqlite3.Connection,
) -> None:
    """Support both current per-key and legacy JSON-blob config schemas."""
    columns = table_columns(connection, "config")
    column_types = table_column_types(connection, "config")
    now = timestamp_for_column(column_types.get("updated_at", ""))

    if {"key", "value"}.issubset(columns):
        sql = (
            "INSERT INTO config (key, value"
            + (", updated_at" if "updated_at" in columns else "")
            + ") VALUES (?, ?"
            + (", ?" if "updated_at" in columns else "")
            + ") ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            + (
                ", updated_at = excluded.updated_at"
                if "updated_at" in columns
                else ""
            )
        )
        values: list[Any] = [
            "ui.default_models",
            json.dumps(MODEL_ID),
        ]
        if "updated_at" in columns:
            values.append(now)
        connection.execute(sql, values)
        return

    if "data" in columns:
        id_column = "id" if "id" in columns else None
        select_columns = (
            f'"{id_column}", data'
            if id_column
            else "rowid, data"
        )
        row = connection.execute(
            f"SELECT {select_columns} FROM config LIMIT 1"
        ).fetchone()
        if not row:
            raise RuntimeError(
                "Open WebUI legacy config 尚未建立"
            )

        row_id, raw_data = row
        data = (
            json.loads(raw_data)
            if isinstance(raw_data, str)
            else dict(raw_data or {})
        )
        ui = data.setdefault("ui", {})
        ui["default_models"] = MODEL_ID
        update_parts = ["data = ?"]
        values = [
            json.dumps(data, ensure_ascii=False),
        ]
        if "updated_at" in columns:
            update_parts.append("updated_at = ?")
            values.append(now)
        values.append(row_id)
        key_name = id_column or "rowid"
        connection.execute(
            "UPDATE config SET "
            + ", ".join(update_parts)
            + f' WHERE "{key_name}" = ?',
            values,
        )
        return

    raise RuntimeError(
        "不支援目前的 Open WebUI config schema"
    )


def install(
    db_path: Path,
    source_path: Path,
) -> None:
    source = source_path.read_text(encoding="utf-8")
    with closing(
        sqlite3.connect(
            db_path,
            timeout=30,
        )
    ) as connection:
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            install_function(connection, source)
            install_default_model(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def is_installed(db_path: Path) -> bool:
    try:
        with closing(
            sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
                timeout=5,
            )
        ) as connection:
            row = connection.execute(
                'SELECT is_active FROM "function" WHERE id = ?',
                (MODEL_ID,),
            ).fetchone()
            return bool(row and row[0])
    except (OSError, sqlite3.Error):
        return False


def wait_and_install(
    db_path: Path,
    source_path: Path,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            if db_path.is_file():
                install(db_path, source_path)
                print(
                    "Aniki 已自動安裝、啟用並設為 Open WebUI 預設模型。",
                    flush=True,
                )
                return
        except (OSError, RuntimeError, sqlite3.Error) as error:
            last_error = error

        time.sleep(2)

    detail = f"：{last_error}" if last_error else ""
    raise TimeoutError(
        f"等待 Open WebUI 資料庫逾時{detail}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="只檢查 Aniki 是否已安裝並啟用",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(
            os.getenv(
                "OPENWEBUI_BOOTSTRAP_TIMEOUT",
                "300",
            )
        ),
    )
    args = parser.parse_args()

    db_path = Path(
        os.getenv(
            "OPENWEBUI_DB_PATH",
            DEFAULT_DB_PATH,
        )
    )
    source_path = Path(
        os.getenv(
            "ANIKI_PIPE_SOURCE",
            DEFAULT_SOURCE_PATH,
        )
    )

    if args.check:
        return 0 if is_installed(db_path) else 1

    try:
        wait_and_install(
            db_path,
            source_path,
            args.timeout,
        )
    except Exception as error:
        print(
            f"Aniki 自動安裝失敗：{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
