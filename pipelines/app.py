# review_routes.py
# ============================================================
# Aniki 人工確認模組：完整可編輯 + 可新增/刪除列欄
#
# 放在原本 FastAPI 主程式旁邊。
#
# 主程式：
#   from review_routes import router as review_router
#   app.include_router(review_router)
#
# 不要單獨執行本檔，也不用另外開 port。
# ============================================================

from __future__ import annotations

import html
import json
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(
    os.getenv(
        "REVIEW_DB_PATH",
        str(BASE_DIR / "data" / "review_tasks.db"),
    )
).expanduser()


# ============================================================
# Pydantic models
# ============================================================

class CreateReviewRequest(BaseModel):
    file_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    columns: list[str]
    rows: list[Any]


class ConfirmReviewRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    columns: list[Any]
    rows: list[list[Any]]


# ============================================================
# Database
# ============================================================

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def get_existing_db_columns(
    connection: sqlite3.Connection,
) -> set[str]:
    info = connection.execute(
        "PRAGMA table_info(review_tasks)"
    ).fetchall()

    return {str(row[1]) for row in info}


def init_review_db() -> None:
    """
    如果之前已經有 review_tasks.db，
    這裡會自動補上新版需要的欄位。
    """
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                file_name TEXT NOT NULL,

                metadata_json TEXT NOT NULL DEFAULT '{}',
                columns_json TEXT NOT NULL,
                original_rows_json TEXT NOT NULL,

                corrected_metadata_json TEXT,
                corrected_columns_json TEXT,
                corrected_rows_json TEXT,

                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                confirmed_at TEXT
            )
            """
        )

        existing = get_existing_db_columns(connection)

        migrations = {
            "metadata_json":
                "ALTER TABLE review_tasks "
                "ADD COLUMN metadata_json TEXT "
                "NOT NULL DEFAULT '{}'",

            "corrected_metadata_json":
                "ALTER TABLE review_tasks "
                "ADD COLUMN corrected_metadata_json TEXT",

            "corrected_columns_json":
                "ALTER TABLE review_tasks "
                "ADD COLUMN corrected_columns_json TEXT",

            "corrected_rows_json":
                "ALTER TABLE review_tasks "
                "ADD COLUMN corrected_rows_json TEXT",
        }

        for column_name, sql in migrations.items():
            if column_name not in existing:
                connection.execute(sql)

        connection.commit()


def create_review_task(
    file_name: str,
    metadata: dict[str, str],
    columns: list[str],
    rows: list[list[str]],
) -> str:
    init_review_db()

    token = secrets.token_urlsafe(24)
    now = datetime.now().isoformat(
        timespec="seconds"
    )

    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO review_tasks (
                token,
                file_name,
                metadata_json,
                columns_json,
                original_rows_json,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                token,
                file_name,
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                ),
                json.dumps(
                    columns,
                    ensure_ascii=False,
                ),
                json.dumps(
                    rows,
                    ensure_ascii=False,
                ),
                now,
            ),
        )

        connection.commit()

    return token


def get_review_task(
    token: str,
) -> sqlite3.Row | None:
    init_review_db()

    with get_db() as connection:
        return connection.execute(
            """
            SELECT *
            FROM review_tasks
            WHERE token = ?
            """,
            (token,),
        ).fetchone()


def save_confirmed_data(
    token: str,
    metadata: dict[str, str],
    columns: list[str],
    rows: list[list[str]],
) -> None:
    now = datetime.now().isoformat(
        timespec="seconds"
    )

    with get_db() as connection:
        cursor = connection.execute(
            """
            UPDATE review_tasks
            SET
                corrected_metadata_json = ?,
                corrected_columns_json = ?,
                corrected_rows_json = ?,
                status = 'confirmed',
                confirmed_at = ?
            WHERE token = ?
              AND status != 'confirmed'
            """,
            (
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                ),
                json.dumps(
                    columns,
                    ensure_ascii=False,
                ),
                json.dumps(
                    rows,
                    ensure_ascii=False,
                ),
                now,
                token,
            ),
        )

        connection.commit()

        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    "這份檢表已經確認過，"
                    "或資料不存在"
                ),
            )


# ============================================================
# Data helpers
# ============================================================

def to_text(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


def normalize_metadata(
    metadata: dict[str, Any],
) -> dict[str, str]:
    return {
        str(key): to_text(value)
        for key, value in metadata.items()
    }


def normalize_columns(
    columns: list[Any],
) -> list[str]:
    result = [
        to_text(column)
        for column in columns
    ]

    if not result:
        raise HTTPException(
            status_code=400,
            detail="至少需要一個欄位",
        )

    empty_indexes = [
        index + 1
        for index, value in enumerate(result)
        if not value
    ]

    if empty_indexes:
        raise HTTPException(
            status_code=400,
            detail=(
                "欄位名稱不能空白。"
                f"空白欄位：{empty_indexes}"
            ),
        )

    return result


def normalize_rows(
    rows: list[Any],
    columns: list[str],
    strict: bool,
) -> list[list[str]]:
    """
    建立 review 時 rows 可用 list[dict] 或 list[list]。
    人工確認送回時使用 list[list]。
    """
    column_count = len(columns)
    result: list[list[str]] = []

    for row_index, row in enumerate(
        rows,
        start=1,
    ):
        if isinstance(row, dict):
            normalized = [
                to_text(
                    row.get(column)
                )
                for column in columns
            ]

        elif isinstance(row, list):
            normalized = [
                to_text(value)
                for value in row
            ]

            if strict:
                if len(normalized) != column_count:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"第 {row_index} 列欄位數錯誤："
                            f"需要 {column_count} 格，"
                            f"收到 {len(normalized)} 格"
                        ),
                    )

            else:
                if len(normalized) < column_count:
                    normalized += (
                        [""] *
                        (
                            column_count
                            - len(normalized)
                        )
                    )

                if len(normalized) > column_count:
                    normalized = (
                        normalized[:column_count]
                    )

        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第 {row_index} 列格式錯誤"
                ),
            )

        result.append(normalized)

    return result


def get_current_review_data(
    task: sqlite3.Row,
) -> tuple[
    dict[str, str],
    list[str],
    list[list[str]],
]:
    # 只要曾經人工確認過，就優先使用最新人工資料。
    # 這樣按「重新編輯」後不會跳回最原始的 VLM 結果。
    if task["corrected_metadata_json"]:
        metadata = json.loads(
            task["corrected_metadata_json"]
        )
    else:
        metadata = json.loads(
            task["metadata_json"]
            or "{}"
        )

    if task["corrected_columns_json"]:
        columns = json.loads(
            task["corrected_columns_json"]
        )
    else:
        columns = json.loads(
            task["columns_json"]
        )

    if task["corrected_rows_json"]:
        rows = json.loads(
            task["corrected_rows_json"]
        )
    else:
        rows = json.loads(
            task["original_rows_json"]
        )

    return metadata, columns, rows


# ============================================================
# HTML building
# ============================================================

def esc(value: Any) -> str:
    return html.escape(
        to_text(value),
        quote=True,
    )


def build_metadata_html(
    metadata: dict[str, str],
    readonly: bool,
) -> str:
    editable = (
        "false"
        if readonly
        else "true"
    )

    if not metadata:
        return """
        <div class="meta-empty">
            此份資料沒有基本資料
        </div>
        """

    parts: list[str] = []

    for key, value in metadata.items():
        remark_class = (
            " remark-value"
            if str(key).strip() == "備註"
            else ""
        )

        parts.append(
            f"""
            <div
                class="meta-label editable-cell meta-key"
                contenteditable="{editable}"
                data-original="{esc(key)}"
                spellcheck="false"
            >{esc(key)}</div>

            <div
                class="meta-value editable-cell{remark_class}"
                contenteditable="{editable}"
                data-original="{esc(value)}"
                spellcheck="false"
            >{esc(value)}</div>
            """
        )

    return (
        '<div class="meta-grid">'
        + "".join(parts)
        + "</div>"
    )


def build_table_html(
    columns: list[str],
    rows: list[list[str]],
    readonly: bool,
) -> str:
    editable = (
        "false"
        if readonly
        else "true"
    )

    headers = "".join(
        f"""
        <th
            class="column-header editable-cell"
            contenteditable="{editable}"
            data-original="{esc(column)}"
            spellcheck="false"
        >{esc(column)}</th>
        """
        for column in columns
    )

    body_parts: list[str] = []

    for row in rows:
        cells = "".join(
            f"""
            <td
                class="data-cell editable-cell"
                contenteditable="{editable}"
                data-original="{esc(value)}"
                spellcheck="false"
            >{esc(value)}</td>
            """
            for value in row
        )

        body_parts.append(
            f"<tr>{cells}</tr>"
        )

    return (
        '<table id="reviewTable">'
        "<thead><tr>"
        + headers
        + "</tr></thead>"
        "<tbody>"
        + "".join(body_parts)
        + "</tbody>"
        "</table>"
    )


HTML_TEMPLATE = r"""
<!doctype html>
<html lang="zh-Hant">

<head>
<meta charset="utf-8">
<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
>

<title>檢表資料確認</title>

<style>
* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    min-height: 100%;
}

body {
    font-family:
        "Microsoft JhengHei",
        "Noto Sans TC",
        Arial,
        sans-serif;
    background: #f3f5f7;
    color: #182132;
}

.page {
    width: min(98vw, 2200px);
    margin: 20px auto 42px;
}

.top-card,
.section {
    background: #ffffff;
    border: 1px solid #cfd6df;
    border-radius: 12px;
    box-shadow:
        0 2px 10px
        rgba(0, 0, 0, 0.04);
}

.top-card {
    padding: 22px 24px;
    margin-bottom: 14px;
}

.section {
    overflow: hidden;
    margin-bottom: 14px;
}

h1 {
    margin: 0 0 10px;
    font-size: 30px;
    font-weight: 900;
}

.file-name {
    font-size: 20px;
    font-weight: 800;
    margin-bottom: 7px;
}

.hint {
    color: #5f6978;
    font-size: 16px;
    line-height: 1.65;
}

.confirmed-notice {
    margin-top: 14px;
    padding: 12px 15px;
    background: #eef9f1;
    border: 1px solid #acd7b7;
    border-radius: 8px;
    font-weight: 800;
}

.section-title {
    padding: 14px 18px;
    font-size: 19px;
    font-weight: 900;
    background: #e8edf2;
    border-bottom: 1px solid #c2cad4;
}

.meta-grid {
    display: grid;
    grid-template-columns:
        minmax(120px, 170px)
        minmax(220px, 1fr)
        minmax(120px, 170px)
        minmax(220px, 1fr);
}

.meta-label,
.meta-value {
    min-height: 58px;
    display: flex;
    align-items: center;
    padding: 10px 14px;
    font-size: 18px;
    border-right: 1px solid #aeb7c2;
    border-bottom: 1px solid #aeb7c2;
}

.meta-label {
    background: #f0f3f6;
    font-weight: 900;
    justify-content: center;
    text-align: center;
}

.meta-value {
    background: #ffffff;
    font-weight: 650;
}

.meta-empty {
    padding: 18px;
    color: #697384;
}

.meta-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    padding: 12px 14px;
    background: #f8fafc;
    border-bottom: 1px solid #cfd6df;
}

.meta-key {
    cursor: text;
}

.meta-pair-selected {
    box-shadow:
        inset 0 0 0 3px #7c3aed !important;
}

.meta-value.remark-value {
    min-height: 96px;
    align-items: flex-start;
    white-space: pre-wrap;
}

.table-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    padding: 12px 14px;
    background: #f8fafc;
    border-bottom: 1px solid #cfd6df;
}

.tool-btn {
    border: 1px solid #aeb7c2;
    background: #ffffff;
    color: #1f2937;
    border-radius: 7px;
    padding: 8px 12px;
    font-size: 14px;
    font-weight: 800;
    cursor: pointer;
}

.tool-btn:hover:not(:disabled) {
    background: #eef4ff;
    border-color: #7098d8;
}

.tool-btn.danger {
    color: #b42318;
}

.tool-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.toolbar-help {
    margin-left: auto;
    color: #687385;
    font-size: 14px;
}

.sheet-wrap {
    width: 100%;
    height: min(60vh, 660px);
    min-height: 360px;
    overflow: auto;
    scrollbar-gutter: stable both-edges;
    overscroll-behavior: contain;
}

table {
    border-collapse: separate;
    border-spacing: 0;
    width: max-content;
    min-width: 100%;
    background: #ffffff;
}

th,
td {
    min-width: 100px;
    height: 48px;
    padding: 0 9px;
    text-align: center;
    vertical-align: middle;
    border-right: 1px solid #aeb7c2;
    border-bottom: 1px solid #aeb7c2;
    font-size: 16px;
    white-space: nowrap;
}

th {
    position: sticky;
    top: 0;
    z-index: 6;
    height: 52px;
    background: #e5eaf0;
    font-weight: 900;
}

th:nth-child(1),
td:nth-child(1) {
    min-width: 66px;
}

th:nth-child(2),
td:nth-child(2) {
    min-width: 95px;
}

th:nth-child(3),
td:nth-child(3),
th:nth-child(4),
td:nth-child(4) {
    min-width: 78px;
}

th:nth-child(5),
td:nth-child(5) {
    min-width: 190px;
    white-space: normal;
}

th:nth-child(6),
td:nth-child(6) {
    min-width: 100px;
}

.editable-cell {
    cursor: text;
}

.editable-cell:hover {
    background: #f6f9ff;
}

.editable-cell:focus {
    outline: none;
    position: relative;
    z-index: 8;
    background: #ffffff;
    box-shadow:
        inset 0 0 0 3px #2563eb;
}

.editable-cell.changed {
    background: #fff3bd;
}

.selected-structure {
    box-shadow:
        inset 0 0 0 3px #7c3aed !important;
}

.actions {
    display: flex;
    align-items: center;
    gap: 14px;
    min-height: 82px;
    padding: 16px 20px;
    background: #fafbfc;
    border-top: 1px solid #d1d8e0;
}

.status {
    margin-right: auto;
    color: #66717f;
    font-size: 17px;
    font-weight: 800;
}

#confirmBtn,
#editBtn {
    border-radius: 9px;
    min-width: 190px;
    padding: 14px 28px;
    font-size: 18px;
    font-weight: 900;
    cursor: pointer;
}

#confirmBtn {
    border: 0;
    background: #2563eb;
    color: white;
}

#confirmBtn:hover:not(:disabled) {
    background: #1d4ed8;
}

#editBtn {
    background: #ffffff;
    color: #1d4ed8;
    border: 2px solid #2563eb;
}

#editBtn:hover {
    background: #eef4ff;
}

#confirmBtn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
}

.success-box {
    display: none;
    margin-top: 14px;
    padding: 20px;
    text-align: center;
    background: #edf9f1;
    border: 1px solid #abd8b7;
    border-radius: 10px;
    font-size: 20px;
    font-weight: 900;
}

@media (max-width: 1000px) {
    .page {
        width: 100%;
        margin: 0;
    }

    .top-card,
    .section {
        border-radius: 0;
    }

    .meta-grid {
        grid-template-columns:
            130px
            minmax(200px, 1fr);
    }

    th,
    td {
        min-width: 110px;
        height: 56px;
        font-size: 18px;
    }

    .toolbar-help {
        width: 100%;
        margin-left: 0;
    }
}
</style>
</head>

<body>

<div class="page">

    <div class="top-card">
        <h1>檢表資料確認</h1>

        <div class="file-name">
            檔案：__FILE_NAME__
        </div>

        <div class="hint">
            整張檢表都可以直接修改。
            也可以新增／刪除資料列與欄位。
            完成後按「確認並送出」。
        </div>

        __NOTICE__
    </div>


    <div class="section">

        <div class="section-title">
            基本資料
        </div>

        <div class="meta-toolbar">

            <button
                class="tool-btn"
                id="addMetaBtn"
                __DISABLED__
            >
                ＋ 新增基本資料欄位
            </button>

            <button
                class="tool-btn danger"
                id="deleteMetaBtn"
                __DISABLED__
            >
                刪除選取基本資料欄位
            </button>

            <div class="toolbar-help">
                點一下欄位名稱或內容即可選取
            </div>

        </div>

        <div id="metaContainer">
            __METADATA_HTML__
        </div>

    </div>


    <div class="section">

        <div class="section-title">
            檢驗表格
        </div>

        <div class="table-toolbar">

            <button
                class="tool-btn"
                id="addRowAboveBtn"
                __DISABLED__
            >
                ＋ 向上新增列
            </button>

            <button
                class="tool-btn"
                id="addRowBelowBtn"
                __DISABLED__
            >
                ＋ 向下新增列
            </button>

            <button
                class="tool-btn"
                id="addColumnLeftBtn"
                __DISABLED__
            >
                ＋ 向左新增欄
            </button>

            <button
                class="tool-btn"
                id="addColumnRightBtn"
                __DISABLED__
            >
                ＋ 向右新增欄
            </button>

            <button
                class="tool-btn danger"
                id="deleteRowBtn"
                __DISABLED__
            >
                刪除選取資料列
            </button>

            <button
                class="tool-btn danger"
                id="deleteColumnBtn"
                __DISABLED__
            >
                刪除選取欄位
            </button>

            <div class="toolbar-help">
                先點選一個格子，再決定向上／下新增列或向左／右新增欄
            </div>

        </div>

        <div class="sheet-wrap">
            __TABLE_HTML__
        </div>

        <div class="actions">

            <div
                class="status"
                id="statusText"
            >
                __STATUS_TEXT__
            </div>

            <button
                id="editBtn"
                style="display: __EDIT_DISPLAY__;"
            >
                重新編輯
            </button>

            <button
                id="confirmBtn"
                __DISABLED__
            >
                確認並送出
            </button>

        </div>

    </div>


    <div
        class="success-box"
        id="successBox"
    >
        ✓ 整張檢表已確認並保存
    </div>

</div>


<script>

const TOKEN = __TOKEN_JSON__;
const INITIAL_STATUS = __STATUS_JSON__;
const READ_ONLY = (
    INITIAL_STATUS === "confirmed"
);

const table = document.getElementById(
    "reviewTable"
);

const confirmBtn = document.getElementById(
    "confirmBtn"
);

const editBtn = document.getElementById(
    "editBtn"
);

const addMetaBtn = document.getElementById(
    "addMetaBtn"
);

const deleteMetaBtn = document.getElementById(
    "deleteMetaBtn"
);

const metaContainer = document.getElementById(
    "metaContainer"
);

const addRowAboveBtn = document.getElementById(
    "addRowAboveBtn"
);

const addRowBelowBtn = document.getElementById(
    "addRowBelowBtn"
);

const addColumnLeftBtn = document.getElementById(
    "addColumnLeftBtn"
);

const addColumnRightBtn = document.getElementById(
    "addColumnRightBtn"
);

const deleteRowBtn = document.getElementById(
    "deleteRowBtn"
);

const deleteColumnBtn = document.getElementById(
    "deleteColumnBtn"
);

const statusText = document.getElementById(
    "statusText"
);

const successBox = document.getElementById(
    "successBox"
);

let selectedRowIndex = null;
let selectedColumnIndex = null;


let selectedMetaPairIndex = null;


function currentMetaCells() {
    return Array.from(
        metaContainer.querySelectorAll(
            ".meta-grid > .meta-label, "
            + ".meta-grid > .meta-value"
        )
    );
}


function currentMetaPairs() {
    const cells = currentMetaCells();
    const pairs = [];

    for (let i = 0; i < cells.length; i += 2) {
        pairs.push({
            key: cells[i] || null,
            value: cells[i + 1] || null
        });
    }

    return pairs;
}


function clearMetaSelection() {
    metaContainer
        .querySelectorAll(".meta-pair-selected")
        .forEach((cell) => {
            cell.classList.remove(
                "meta-pair-selected"
            );
        });
}


function selectMetaPair(index) {
    selectedMetaPairIndex = index;
    clearMetaSelection();

    const pair = currentMetaPairs()[index];

    if (!pair) {
        return;
    }

    if (pair.key) {
        pair.key.classList.add(
            "meta-pair-selected"
        );
    }

    if (pair.value) {
        pair.value.classList.add(
            "meta-pair-selected"
        );
    }
}


function ensureMetaGrid() {
    let grid = metaContainer.querySelector(
        ".meta-grid"
    );

    if (!grid) {
        metaContainer.innerHTML = (
            '<div class="meta-grid"></div>'
        );

        grid = metaContainer.querySelector(
            ".meta-grid"
        );
    }

    return grid;
}


function addMetadataField() {
    if (READ_ONLY) {
        return;
    }

    let key = window.prompt(
        "請輸入基本資料欄位名稱：",
        "備註"
    );

    if (key === null) {
        return;
    }

    key = normalizeText(key);

    if (!key) {
        alert("欄位名稱不能空白");
        return;
    }

    const grid = ensureMetaGrid();

    const label = document.createElement(
        "div"
    );
    label.className = (
        "meta-label editable-cell meta-key changed"
    );
    label.contentEditable = "true";
    label.dataset.original = "";
    label.spellcheck = false;
    label.innerText = key;

    const value = document.createElement(
        "div"
    );
    value.className = (
        "meta-value editable-cell changed"
    );
    value.contentEditable = "true";
    value.dataset.original = "";
    value.spellcheck = false;
    value.innerText = "";

    if (key === "備註") {
        value.classList.add(
            "remark-value"
        );
    }

    grid.appendChild(label);
    grid.appendChild(value);

    const pairs = currentMetaPairs();
    selectMetaPair(
        pairs.length - 1
    );

    focusCell(value);

    statusText.innerText = (
        "已新增基本資料欄位，尚未送出"
    );
}


function deleteSelectedMetadataField() {
    if (READ_ONLY) {
        return;
    }

    const pairs = currentMetaPairs();

    if (
        selectedMetaPairIndex === null
        || !pairs[selectedMetaPairIndex]
    ) {
        alert(
            "請先點一下要刪除的基本資料欄位"
        );
        return;
    }

    const pair = pairs[
        selectedMetaPairIndex
    ];

    const keyName = pair.key
        ? normalizeText(pair.key.innerText)
        : "";

    const ok = window.confirm(
        "確定要刪除基本資料欄位「"
        + keyName
        + "」嗎？"
    );

    if (!ok) {
        return;
    }

    if (pair.key) {
        pair.key.remove();
    }

    if (pair.value) {
        pair.value.remove();
    }

    selectedMetaPairIndex = null;
    clearMetaSelection();

    statusText.innerText = (
        "已刪除基本資料欄位，尚未送出"
    );
}


metaContainer.addEventListener(
    "click",
    (event) => {
        const cell = event.target.closest(
            ".meta-label, .meta-value"
        );

        if (!cell) {
            return;
        }

        const cells = currentMetaCells();
        const index = cells.indexOf(cell);

        if (index >= 0) {
            selectMetaPair(
                Math.floor(index / 2)
            );
        }
    }
);


function normalizeText(text) {
    return text
        .replace(/\u00A0/g, " ")
        .replace(/\r?\n/g, " ")
        .replace(/\s+/g, " ")
        .trim();
}


function markChanged(cell) {
    const current = normalizeText(
        cell.innerText
    );

    const original = normalizeText(
        cell.dataset.original || ""
    );

    if (current !== original) {
        cell.classList.add(
            "changed"
        );
    } else {
        cell.classList.remove(
            "changed"
        );
    }
}


function placeCursorAtEnd(element) {
    const range = document.createRange();
    const selection = window.getSelection();

    range.selectNodeContents(element);
    range.collapse(false);

    selection.removeAllRanges();
    selection.addRange(range);
}


function focusCell(cell) {
    if (!cell) {
        return;
    }

    if (
        cell.getAttribute(
            "contenteditable"
        ) !== "true"
    ) {
        return;
    }

    cell.focus();
    placeCursorAtEnd(cell);
}


function currentHeaders() {
    return Array.from(
        table.querySelectorAll(
            "thead th"
        )
    );
}


function currentRows() {
    return Array.from(
        table.querySelectorAll(
            "tbody tr"
        )
    );
}


function currentCells() {
    return Array.from(
        table.querySelectorAll(
            "tbody td"
        )
    );
}


function clearStructureSelection() {
    table
        .querySelectorAll(
            ".selected-structure"
        )
        .forEach((cell) => {
            cell.classList.remove(
                "selected-structure"
            );
        });
}


function selectStructure(
    rowIndex,
    columnIndex
) {
    selectedRowIndex = rowIndex;
    selectedColumnIndex = columnIndex;

    clearStructureSelection();

    const rows = currentRows();
    const headers = currentHeaders();

    if (
        rowIndex !== null
        && rows[rowIndex]
    ) {
        Array.from(
            rows[rowIndex].children
        ).forEach((cell) => {
            cell.classList.add(
                "selected-structure"
            );
        });
    }

    if (
        columnIndex !== null
        && headers[columnIndex]
    ) {
        headers[columnIndex].classList.add(
            "selected-structure"
        );

        rows.forEach((row) => {
            if (row.children[columnIndex]) {
                row.children[columnIndex]
                    .classList.add(
                        "selected-structure"
                    );
            }
        });
    }
}


function createEditableCell() {
    const td = document.createElement(
        "td"
    );

    td.className = (
        "data-cell editable-cell changed"
    );

    td.contentEditable = "true";
    td.dataset.original = "";
    td.spellcheck = false;
    td.innerText = "";

    return td;
}


function createEditableHeader(name) {
    const th = document.createElement(
        "th"
    );

    th.className = (
        "column-header editable-cell changed"
    );

    th.contentEditable = "true";
    th.dataset.original = "";
    th.spellcheck = false;
    th.innerText = name;

    return th;
}


function requireSelectedRow() {
    const rows = currentRows();

    if (
        selectedRowIndex === null
        || !rows[selectedRowIndex]
    ) {
        alert(
            "請先點一下表格中要作為基準的資料列"
        );
        return null;
    }

    return selectedRowIndex;
}


function requireSelectedColumn() {
    const headers = currentHeaders();

    if (
        selectedColumnIndex === null
        || !headers[selectedColumnIndex]
    ) {
        alert(
            "請先點一下表格中要作為基準的欄位"
        );
        return null;
    }

    return selectedColumnIndex;
}


function buildEmptyRow() {
    const columnCount = (
        currentHeaders().length
    );

    const tr = document.createElement(
        "tr"
    );

    for (
        let i = 0;
        i < columnCount;
        i += 1
    ) {
        tr.appendChild(
            createEditableCell()
        );
    }

    return tr;
}


function addRowAbove() {
    if (READ_ONLY) {
        return;
    }

    const rowIndex = requireSelectedRow();

    if (rowIndex === null) {
        return;
    }

    const tbody = table.querySelector(
        "tbody"
    );

    const rows = currentRows();
    const newRow = buildEmptyRow();

    tbody.insertBefore(
        newRow,
        rows[rowIndex]
    );

    selectStructure(
        rowIndex,
        0
    );

    focusCell(
        newRow.children[0]
    );

    statusText.innerText = (
        "已在選取列上方新增資料列，尚未送出"
    );
}


function addRowBelow() {
    if (READ_ONLY) {
        return;
    }

    const rowIndex = requireSelectedRow();

    if (rowIndex === null) {
        return;
    }

    const tbody = table.querySelector(
        "tbody"
    );

    const rows = currentRows();
    const newRow = buildEmptyRow();

    const nextRow = rows[
        rowIndex + 1
    ];

    if (nextRow) {
        tbody.insertBefore(
            newRow,
            nextRow
        );
    } else {
        tbody.appendChild(
            newRow
        );
    }

    selectStructure(
        rowIndex + 1,
        0
    );

    focusCell(
        newRow.children[0]
    );

    statusText.innerText = (
        "已在選取列下方新增資料列，尚未送出"
    );
}


function askNewColumnName() {
    let name = window.prompt(
        "請輸入新欄位名稱：",
        "新欄位"
    );

    if (name === null) {
        return null;
    }

    name = normalizeText(name);

    if (!name) {
        alert(
            "欄位名稱不能空白"
        );
        return null;
    }

    return name;
}


function insertColumnAt(
    insertIndex,
    name
) {
    const headerRow = table.querySelector(
        "thead tr"
    );

    const headersBefore = (
        currentHeaders()
    );

    const newHeader = (
        createEditableHeader(name)
    );

    const headerReference = (
        headersBefore[insertIndex]
        || null
    );

    headerRow.insertBefore(
        newHeader,
        headerReference
    );

    currentRows().forEach((row) => {
        const newCell = (
            createEditableCell()
        );

        const referenceCell = (
            row.children[insertIndex]
            || null
        );

        row.insertBefore(
            newCell,
            referenceCell
        );
    });

    selectStructure(
        null,
        insertIndex
    );

    focusCell(
        newHeader
    );
}


function addColumnLeft() {
    if (READ_ONLY) {
        return;
    }

    const columnIndex = (
        requireSelectedColumn()
    );

    if (columnIndex === null) {
        return;
    }

    const name = askNewColumnName();

    if (name === null) {
        return;
    }

    insertColumnAt(
        columnIndex,
        name
    );

    statusText.innerText = (
        "已在選取欄左側新增欄位，尚未送出"
    );
}


function addColumnRight() {
    if (READ_ONLY) {
        return;
    }

    const columnIndex = (
        requireSelectedColumn()
    );

    if (columnIndex === null) {
        return;
    }

    const name = askNewColumnName();

    if (name === null) {
        return;
    }

    insertColumnAt(
        columnIndex + 1,
        name
    );

    statusText.innerText = (
        "已在選取欄右側新增欄位，尚未送出"
    );
}


function deleteSelectedRow() {
    if (READ_ONLY) {
        return;
    }

    const rows = currentRows();

    if (
        selectedRowIndex === null
        || !rows[selectedRowIndex]
    ) {
        alert(
            "請先點一下要刪除的資料列"
        );
        return;
    }

    const ok = window.confirm(
        "確定要刪除目前選取的資料列嗎？"
    );

    if (!ok) {
        return;
    }

    rows[selectedRowIndex].remove();

    selectedRowIndex = null;
    selectedColumnIndex = null;
    clearStructureSelection();

    statusText.innerText = (
        "已刪除資料列，尚未送出"
    );
}


function deleteSelectedColumn() {
    if (READ_ONLY) {
        return;
    }

    const headers = currentHeaders();

    if (
        selectedColumnIndex === null
        || !headers[selectedColumnIndex]
    ) {
        alert(
            "請先點一下要刪除的欄位"
        );
        return;
    }

    if (headers.length <= 1) {
        alert(
            "至少要保留一個欄位"
        );
        return;
    }

    const columnName = normalizeText(
        headers[
            selectedColumnIndex
        ].innerText
    );

    const ok = window.confirm(
        "確定要刪除欄位「"
        + columnName
        + "」嗎？"
    );

    if (!ok) {
        return;
    }

    headers[
        selectedColumnIndex
    ].remove();

    currentRows().forEach((row) => {
        if (
            row.children[
                selectedColumnIndex
            ]
        ) {
            row.children[
                selectedColumnIndex
            ].remove();
        }
    });

    selectedRowIndex = null;
    selectedColumnIndex = null;
    clearStructureSelection();

    statusText.innerText = (
        "已刪除欄位，尚未送出"
    );
}


table.addEventListener(
    "click",
    (event) => {
        const cell = event.target.closest(
            "td, th"
        );

        if (!cell) {
            return;
        }

        if (cell.tagName === "TH") {
            const headers = (
                currentHeaders()
            );

            selectStructure(
                null,
                headers.indexOf(cell)
            );

            return;
        }

        const row = cell.parentElement;
        const rows = currentRows();

        selectStructure(
            rows.indexOf(row),
            Array.from(
                row.children
            ).indexOf(cell)
        );
    }
);


document.addEventListener(
    "input",
    (event) => {
        const cell = event.target.closest(
            ".editable-cell"
        );

        if (cell) {
            markChanged(cell);
        }
    }
);


document.addEventListener(
    "paste",
    (event) => {
        const cell = event.target.closest(
            ".editable-cell"
        );

        if (!cell) {
            return;
        }

        event.preventDefault();

        const clipboard = (
            event.clipboardData
            || window.clipboardData
        );

        const text = clipboard
            .getData("text")
            .replace(/\r?\n/g, " ");

        document.execCommand(
            "insertText",
            false,
            text
        );
    }
);


table.addEventListener(
    "keydown",
    (event) => {
        if (READ_ONLY) {
            return;
        }

        const cell = event.target.closest(
            "td, th"
        );

        if (!cell) {
            return;
        }

        if (event.key === "Tab") {
            event.preventDefault();

            const all = [
                ...currentHeaders(),
                ...currentCells()
            ];

            const index = all.indexOf(cell);
            const direction = (
                event.shiftKey ? -1 : 1
            );

            const target = (
                all[index + direction]
            );

            focusCell(target);
            return;
        }

        if (
            event.key === "Enter"
            && cell.tagName === "TD"
        ) {
            event.preventDefault();

            const row = cell.parentElement;
            const rows = currentRows();

            const rowIndex = (
                rows.indexOf(row)
            );

            const columnIndex = (
                Array.from(
                    row.children
                ).indexOf(cell)
            );

            if (
                rows[rowIndex + 1]
                && rows[rowIndex + 1]
                    .children[columnIndex]
            ) {
                focusCell(
                    rows[rowIndex + 1]
                        .children[columnIndex]
                );
            }
        }
    }
);


function readMetadata() {
    const metadata = {};

    currentMetaPairs().forEach((pair) => {
        if (!pair.key) {
            return;
        }

        const key = normalizeText(
            pair.key.innerText
        );

        const value = pair.value
            ? normalizeText(
                pair.value.innerText
            )
            : "";

        if (key) {
            metadata[key] = value;
        }
    });

    return metadata;
}


function readColumns() {
    return currentHeaders().map(
        (header) => (
            normalizeText(
                header.innerText
            )
        )
    );
}


function readRows() {
    return currentRows().map(
        (row) => (
            Array.from(
                row.querySelectorAll("td")
            ).map(
                (cell) => (
                    normalizeText(
                        cell.innerText
                    )
                )
            )
        )
    );
}


async function reopenForEditing() {
    const ok = window.confirm(
        "要重新開放這份檢表進行修改嗎？"
    );

    if (!ok) {
        return;
    }

    editBtn.disabled = true;
    editBtn.innerText = "開啟中…";

    try {
        const response = await fetch(
            "/api/reviews/"
            + encodeURIComponent(TOKEN)
            + "/edit",
            {
                method: "POST"
            }
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail
                || "重新開啟失敗"
            );
        }

        window.location.reload();

    } catch (error) {
        alert(
            "重新開啟失敗："
            + error.message
        );

        editBtn.disabled = false;
        editBtn.innerText = "重新編輯";
    }
}


async function confirmData() {
    const columns = readColumns();

    if (
        columns.some(
            (column) => !column
        )
    ) {
        alert(
            "欄位名稱不能空白，"
            + "請先補上欄位名稱。"
        );
        return;
    }

    const ok = window.confirm(
        "確定整張檢表都已確認完成，"
        + "並送出嗎？"
    );

    if (!ok) {
        return;
    }

    confirmBtn.disabled = true;
    confirmBtn.innerText = "送出中…";
    statusText.innerText = "正在保存";

    try {
        const response = await fetch(
            "/api/reviews/"
            + encodeURIComponent(TOKEN)
            + "/confirm",
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify({
                    metadata: readMetadata(),
                    columns: columns,
                    rows: readRows()
                })
            }
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail
                || "儲存失敗"
            );
        }

        statusText.innerText = "已確認";
        confirmBtn.innerText = "已送出";
        confirmBtn.disabled = true;

        editBtn.style.display = "inline-block";
        editBtn.disabled = false;
        editBtn.innerText = "重新編輯";

        [
            addMetaBtn,
            deleteMetaBtn,
            addRowAboveBtn,
            addRowBelowBtn,
            addColumnLeftBtn,
            addColumnRightBtn,
            deleteRowBtn,
            deleteColumnBtn
        ].forEach((button) => {
            button.disabled = true;
        });

        document
            .querySelectorAll(
                ".editable-cell"
            )
            .forEach((cell) => {
                cell.contentEditable = (
                    "false"
                );
            });

        clearStructureSelection();

        successBox.style.display = (
            "block"
        );

    } catch (error) {
        alert(
            "送出失敗："
            + error.message
        );

        statusText.innerText = (
            "送出失敗"
        );

        confirmBtn.disabled = false;
        confirmBtn.innerText = (
            "確認並送出"
        );
    }
}


addMetaBtn.addEventListener(
    "click",
    addMetadataField
);

deleteMetaBtn.addEventListener(
    "click",
    deleteSelectedMetadataField
);

addRowAboveBtn.addEventListener(
    "click",
    addRowAbove
);

addRowBelowBtn.addEventListener(
    "click",
    addRowBelow
);

addColumnLeftBtn.addEventListener(
    "click",
    addColumnLeft
);

addColumnRightBtn.addEventListener(
    "click",
    addColumnRight
);

deleteRowBtn.addEventListener(
    "click",
    deleteSelectedRow
);

deleteColumnBtn.addEventListener(
    "click",
    deleteSelectedColumn
);

editBtn.addEventListener(
    "click",
    reopenForEditing
);

confirmBtn.addEventListener(
    "click",
    confirmData
);

</script>

</body>
</html>
"""


def build_review_html(
    file_name: str,
    metadata: dict[str, str],
    columns: list[str],
    rows: list[list[str]],
    token: str,
    status: str,
) -> str:
    readonly = (
        status == "confirmed"
    )

    notice = ""

    if readonly:
        notice = """
        <div class="confirmed-notice">
            此檢表已完成確認，目前為唯讀狀態。
        </div>
        """

    disabled = (
        "disabled"
        if readonly
        else ""
    )

    result = HTML_TEMPLATE

    replacements = {
        "__FILE_NAME__":
            esc(file_name),

        "__NOTICE__":
            notice,

        "__METADATA_HTML__":
            build_metadata_html(
                metadata,
                readonly,
            ),

        "__TABLE_HTML__":
            build_table_html(
                columns,
                rows,
                readonly,
            ),

        "__DISABLED__":
            disabled,

        "__STATUS_TEXT__":
            (
                "已確認"
                if readonly
                else (
                    "重新編輯中"
                    if status == "editing"
                    else "尚未送出"
                )
            ),

        "__EDIT_DISPLAY__":
            (
                "inline-block"
                if readonly
                else "none"
            ),

        "__TOKEN_JSON__":
            json.dumps(
                token,
                ensure_ascii=False,
            ),

        "__STATUS_JSON__":
            json.dumps(
                status,
                ensure_ascii=False,
            ),
    }

    for key, value in replacements.items():
        result = result.replace(
            key,
            value,
        )

    return result


def reopen_review_task(
    token: str,
) -> None:
    """
    將已確認資料重新開放編輯。
    corrected_* 內容保留，作為重新編輯的起點。
    """
    with get_db() as connection:
        cursor = connection.execute(
            """
            UPDATE review_tasks
            SET
                status = 'editing',
                confirmed_at = NULL
            WHERE token = ?
              AND status = 'confirmed'
            """,
            (token,),
        )

        connection.commit()

        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    "只有已確認的檢表"
                    "才能重新開放編輯"
                ),
            )


# ============================================================
# API routes
# ============================================================

@router.post("/api/reviews")
def api_create_review(
    payload: CreateReviewRequest,
    request: Request,
) -> dict[str, Any]:
    file_name = payload.file_name.strip()

    if not file_name:
        raise HTTPException(
            status_code=400,
            detail="file_name 不能為空",
        )

    columns = normalize_columns(
        payload.columns
    )

    metadata = normalize_metadata(
        payload.metadata
    )

    rows = normalize_rows(
        payload.rows,
        columns,
        strict=False,
    )

    token = create_review_task(
        file_name=file_name,
        metadata=metadata,
        columns=columns,
        rows=rows,
    )

    base_url = str(
        request.base_url
    ).rstrip("/")

    return {
        "status": "created",
        "token": token,
        "review_url": (
            f"{base_url}/review/{token}"
        ),
    }


@router.get(
    "/review/{token}",
    response_class=HTMLResponse,
)
def review_page(
    token: str,
) -> HTMLResponse:
    task = get_review_task(token)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="找不到這份人工確認資料",
        )

    metadata, columns, rows = (
        get_current_review_data(task)
    )

    return HTMLResponse(
        build_review_html(
            file_name=task["file_name"],
            metadata=metadata,
            columns=columns,
            rows=rows,
            token=token,
            status=task["status"],
        )
    )


@router.post(
    "/api/reviews/{token}/confirm"
)
def api_confirm_review(
    token: str,
    payload: ConfirmReviewRequest,
) -> dict[str, Any]:
    task = get_review_task(token)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="找不到這份人工確認資料",
        )

    if task["status"] == "confirmed":
        raise HTTPException(
            status_code=409,
            detail="這份檢表已經確認過",
        )

    columns = normalize_columns(
        payload.columns
    )

    metadata = normalize_metadata(
        payload.metadata
    )

    rows = normalize_rows(
        payload.rows,
        columns,
        strict=True,
    )

    save_confirmed_data(
        token=token,
        metadata=metadata,
        columns=columns,
        rows=rows,
    )

    return {
        "status": "confirmed",
        "token": token,
        "file_name": task["file_name"],
        "metadata": metadata,
        "columns": columns,
        "rows": rows,
    }


@router.post(
    "/api/reviews/{token}/edit"
)
def api_reopen_review(
    token: str,
) -> dict[str, Any]:
    task = get_review_task(token)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="找不到這份人工確認資料",
        )

    reopen_review_task(token)

    return {
        "status": "editing",
        "token": token,
        "message": "已重新開放編輯",
    }


@router.get(
    "/api/reviews/{token}"
)
def api_get_review(
    token: str,
) -> dict[str, Any]:
    task = get_review_task(token)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="找不到這份人工確認資料",
        )

    metadata, columns, rows = (
        get_current_review_data(task)
    )

    return {
        "token": token,
        "file_name": task["file_name"],
        "metadata": metadata,
        "columns": columns,
        "rows": rows,
        "status": task["status"],
        "created_at": task["created_at"],
        "confirmed_at": task["confirmed_at"],
    }


# ============================================================
# Demo
# ============================================================

@router.get("/review-demo/create")
def create_demo_review(
    request: Request,
) -> dict[str, Any]:
    metadata = {
        "公司名稱": "惠傑工業股份有限公司",
        "日期": "115年7月14日",
        "架刀人員": "",
        "客戶": "時碩",
        "品名/圖號": "791004",
        "Rev.": "E",
        "第二工程": "車1",
        "機台編號": "C8",
        "檢查時間": "",
        "品號": "791004",
        "製程": "車1",
        "備註": "",
    }

    columns = [
        "編號",
        "量具編號",
        "代號",
        "刀號",
        "檢驗標準",
        "公差",
        "首件",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
    ]

    # 完全照 VLM rows 的輸出順序，不依編號排序。
    rows = [
        {"編號":"1","量具編號":"eye1","代號":"D","刀號":"","檢驗標準":"該對樣品、首件或末件","公差":"","首件":"✓"},
        {"編號":"2","量具編號":"eye1","代號":"H","刀號":"","檢驗標準":"夾持位置","公差":"","首件":"✓","1":"✓","2":"✓","3":"✓","4":"✓","5":"✓"},
        {"編號":"3","量具編號":"eye1","代號":"F","刀號":"","檢驗標準":"擺放方式","公差":""},
        {"編號":"4","量具編號":"eye1","代號":"H","刀號":"","檢驗標準":"外觀毛邊","公差":"","首件":"✓","1":"✓","2":"✓","3":"✓","4":"✓","5":"✓"},
        {"編號":"5","量具編號":"M","代號":"F","刀號":"","檢驗標準":"檢驗標準","公差":""},
        {"編號":"6","量具編號":"H","代號":"H","刀號":"","檢驗標準":"211","公差":"±0.3","首件":"10.5","1":"10.5","2":"21","3":"10.5","4":"10.5","5":"1234567890"},
        {"編號":"10","量具編號":"M","代號":"H","刀號":"T2","檢驗標準":"ø53.975","公差":"±0.03","首件":"1397","1":"53.97","2":"3.71","3":"53.97","4":"5297","5":"8791"},
        {"編號":"9","量具編號":"BG","代號":"H","刀號":"T6","檢驗標準":"ø47.66°","公差":"±0","首件":"0.64","1":"47.09","2":"410","3":"47.84","4":"1234567890","5":"94"},
        {"編號":"11","量具編號":"PM","代號":"H","刀號":"T5","檢驗標準":"ø50.169","公差":"±0.1","首件":"50.17","1":"-0.17","2":"-50.8","3":"50.13","4":"0.125","5":"50.16"},
        {"編號":"12","量具編號":"PM","代號":"H","刀號":"3","檢驗標準":"ø51.175","公差":"±0.1","首件":"50.7","1":"57.2","2":"5/2","3":"512","4":"5/0","5":"51.7"},
        {"編號":"13","量具編號":"PF","代號":"D","刀號":"","檢驗標準":"30°","公差":"±1°","首件":"27.0"},
        {"編號":"14","量具編號":"PF","代號":"D","刀號":"","檢驗標準":"1.295","公差":"±0.1","首件":"13"},
        {"編號":"15","量具編號":"PF","代號":"D","刀號":"","檢驗標準":"30°","公差":"±1°","首件":"3.0"},
        {"編號":"16","量具編號":"PF","代號":"D","刀號":"","檢驗標準":"不可有毛邊","公差":"","首件":"✓"},
        {"編號":"17","量具編號":"PF","代號":"D","刀號":"","檢驗標準":"不可有毛邊","公差":"","首件":"✓"},
        {"編號":"7","量具編號":"H","代號":"C","刀號":"","檢驗標準":"105","公差":"±0.1Ra(以上)"},
        {"編號":"8","量具編號":"H","代號":"C","刀號":"","檢驗標準":"105","公差":"±Ra(以上)"},
    ]

    normalized_rows = normalize_rows(
        rows,
        columns,
        strict=False,
    )

    token = create_review_task(
        file_name="檢表5.pdf",
        metadata=metadata,
        columns=columns,
        rows=normalized_rows,
    )

    base_url = str(
        request.base_url
    ).rstrip("/")

    return {
        "status": "created",
        "token": token,
        "review_url": (
            f"{base_url}/review/{token}"
        ),
    }
