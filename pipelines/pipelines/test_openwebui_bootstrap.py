import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import openwebui_bootstrap as bootstrap


CURRENT_SCHEMA = """
CREATE TABLE user (
    id TEXT PRIMARY KEY,
    role TEXT,
    created_at INTEGER
);
CREATE TABLE function (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    meta TEXT,
    valves TEXT,
    is_active BOOLEAN,
    is_global BOOLEAN,
    updated_at INTEGER,
    created_at INTEGER
);
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value JSON NOT NULL,
    updated_at INTEGER
);
"""


class BootstrapTests(unittest.TestCase):
    def test_install_and_update_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "webui.db"
            source_path = root / "pipe.py"
            source_path.write_text("class Pipe: pass\n", encoding="utf-8")

            with closing(sqlite3.connect(db_path)) as connection:
                connection.executescript(CURRENT_SCHEMA)
                connection.execute(
                    "INSERT INTO user (id, role, created_at) VALUES (?, ?, ?)",
                    ("admin-id", "admin", 1),
                )
                connection.commit()

            bootstrap.install(db_path, source_path)

            with closing(sqlite3.connect(db_path)) as connection:
                function = connection.execute(
                    "SELECT user_id, name, type, content, meta, is_active, valves "
                    "FROM function WHERE id = ?",
                    (bootstrap.MODEL_ID,),
                ).fetchone()
                default_model = connection.execute(
                    "SELECT value FROM config WHERE key = 'ui.default_models'"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE function SET valves = ? WHERE id = ?",
                    ('{"DEFAULT_PAGES":"1"}', bootstrap.MODEL_ID),
                )
                connection.commit()

            self.assertEqual(function[0:4], (
                "admin-id",
                "Aniki",
                "pipe",
                "class Pipe: pass\n",
            ))
            self.assertEqual(json.loads(function[4])["manifest"]["title"], "Aniki")
            self.assertEqual(function[5], 1)
            self.assertIsNone(function[6])
            self.assertEqual(json.loads(default_model), "aniki")
            self.assertTrue(bootstrap.is_installed(db_path))

            source_path.write_text("class Pipe: updated = True\n", encoding="utf-8")
            bootstrap.install(db_path, source_path)
            with closing(sqlite3.connect(db_path)) as connection:
                updated = connection.execute(
                    "SELECT content, valves FROM function WHERE id = ?",
                    (bootstrap.MODEL_ID,),
                ).fetchone()

            self.assertEqual(updated[0], "class Pipe: updated = True\n")
            self.assertEqual(updated[1], '{"DEFAULT_PAGES":"1"}')

    def test_legacy_config_blob_is_updated(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                "CREATE TABLE config ("
                "id INTEGER PRIMARY KEY, data TEXT, updated_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO config (id, data) VALUES (1, ?)",
                ('{"ui":{"default_models":"old"}}',),
            )

            bootstrap.install_default_model(connection)
            raw = connection.execute(
                "SELECT data FROM config WHERE id = 1"
            ).fetchone()[0]

        self.assertEqual(
            json.loads(raw)["ui"]["default_models"],
            "aniki",
        )


if __name__ == "__main__":
    unittest.main()
