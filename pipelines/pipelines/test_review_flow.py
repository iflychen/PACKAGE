"""SQLite review flow using the same values accepted by OCR and human edits."""
from pathlib import Path
from tempfile import TemporaryDirectory

import app


def test_value_states_and_derived_accuracy():
    with TemporaryDirectory() as temp_dir:
        app.DB_PATH = Path(temp_dir) / "nested" / "review_tasks.db"
        app.init_review_db()
        columns = ["頁碼", "項目", "實際值", "首件", "複核"]
        original_rows = [
            ["1", "A", "unknown", "unclear", None],
            ["2", "B", "12.3000", "✓", None],
        ]
        token = app.create_review_task("mixed.pdf", {}, columns, original_rows)
        task = app.get_review_task(token)
        row_keys, column_keys = app.get_review_keys(task, 2, 5)
        corrected_rows = [
            ["1", "A", "10.2000", "unclear", None],
            ["2", "B", "12.3000", "✓", None],
        ]

        evaluations = []
        expected = [
            ["correct", "incorrect", "unclear", "blank"],
            ["correct", "correct", "correct", "blank"],
        ]
        for row_index, verdicts in enumerate(expected):
            for verdict_index, column_index in enumerate((1, 2, 3, 4)):
                evaluations.append({
                    "row_key": row_keys[row_index],
                    "column_key": column_keys[column_index],
                    "verdict": verdicts[verdict_index],
                })

        app.save_confirmed_data(
            token, {}, columns, corrected_rows, row_keys, column_keys, evaluations
        )
        confirmed = app.get_review_task(token)
        summary = app.get_accuracy_summary(confirmed["id"])
        assert (summary["correct"], summary["incorrect"], summary["unclear"], summary["blank"]) == (4, 1, 1, 2)
        assert summary["scored_total"] == 5
        assert summary["custom_accuracy_percent"] == 80.0

        stored = app.get_cell_labels(confirmed["id"])
        assert stored[("orig:0", "col:2")] == "incorrect"
        assert stored[("orig:0", "col:3")] == "unclear"
        assert stored[("orig:0", "col:4")] == "blank"
        with app.get_db() as db:
            values = {
                (row["row_key"], row["column_name"]): (row["model_value"], row["corrected_value"])
                for row in db.execute(
                    "SELECT row_key,column_name,model_value,corrected_value "
                    "FROM review_cell_evaluations WHERE review_task_id=?",
                    (confirmed["id"],),
                )
            }
        assert values[("orig:0", "col:2")] == ("unknown", "10.2000")
        assert values[("orig:0", "col:3")] == ("unclear", "unclear")
        assert values[("orig:0", "col:4")] == (None, None)
        assert values[("orig:1", "col:3")] == ("✓", "✓")

        page = app.build_review_html(
            "mixed.pdf", {}, columns, corrected_rows, token,
            "confirmed", row_keys, column_keys, original_rows, stored,
        )
        for marker in ('data-marker="unknown"', 'data-marker="unclear"',
                       'data-marker="null"', 'data-marker="✓"'):
            assert marker in page


def test_legacy_verdict_schema_upgrade():
    with TemporaryDirectory() as temp_dir:
        app.DB_PATH = Path(temp_dir) / "review_tasks.db"
        token = app.create_review_task("legacy.pdf", {}, ["頁碼", "值"], [["1", "12.0"]])
        task = app.get_review_task(token)
        with app.get_db() as db:
            db.execute("DROP TABLE review_cell_evaluations")
            db.executescript("""
                CREATE TABLE review_cell_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_task_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    row_key TEXT NOT NULL,
                    column_name TEXT NOT NULL,
                    display_column_name TEXT NOT NULL DEFAULT '',
                    model_value TEXT,
                    corrected_value TEXT,
                    verdict TEXT NOT NULL CHECK(verdict IN ('correct','incorrect')),
                    reviewed_at TEXT NOT NULL
                );
                INSERT INTO review_cell_evaluations
                  (review_task_id,page_number,row_key,column_name,display_column_name,
                   model_value,corrected_value,verdict,reviewed_at)
                VALUES (1,1,'orig:0','col:1','值','12.0','12.0','correct','before-upgrade');
            """)
        app.init_review_db()
        with app.get_db() as db:
            schema = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_cell_evaluations'"
            ).fetchone()[0]
            preserved = db.execute(
                "SELECT model_value,corrected_value,verdict FROM review_cell_evaluations WHERE review_task_id=?",
                (task["id"],),
            ).fetchone()
        assert "unclear" in schema and "blank" in schema
        assert tuple(preserved) == ("12.0", "12.0", "correct")


if __name__ == "__main__":
    test_value_states_and_derived_accuracy()
    test_legacy_verdict_schema_upgrade()
    print("PASS: SQLite values, markers, derived verdicts, legacy schema upgrade, and review UI")
