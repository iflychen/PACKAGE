"""Convert Aniki's page output into the editable review-table shape."""

from __future__ import annotations

from pathlib import Path
from typing import Any


CMM_COLUMNS = [
    "頁碼",
    "項目",
    "实际值",
    "名义值",
    "上公差",
    "下公差",
    "偏差",
]

FIELD_ALIASES = {
    "項目": ("項目", "项目", "項目名稱", "项目名称", "特性名稱", "特性名称"),
    "实际值": ("实际值", "實際值"),
    "名义值": ("名义值", "名義值"),
    "上公差": ("上公差",),
    "下公差": ("下公差",),
    "偏差": ("偏差",),
}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"null", "none"} else text


def _row_mapping(row: Any, headers: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {str(key).strip(): value for key, value in row.items()}
    if isinstance(row, list):
        return {
            str(headers[index]).strip(): value
            for index, value in enumerate(row)
            if index < len(headers)
        }
    return {}


def _find_value(row: dict[str, Any], canonical_name: str) -> Any:
    for alias in FIELD_ALIASES[canonical_name]:
        if alias in row:
            return row[alias]
    return None


def make_review_payload(
    *,
    file_name: str,
    metadata: dict[str, Any],
    pages: list[Any],
) -> dict[str, Any]:
    """Flatten page results into one review table while retaining page numbers.

    Empty OCR results are allowed so the upload can still open a review task and
    the operator can add missing rows in the page.
    """
    normalized_pages = [page for page in pages if isinstance(page, dict)]
    discovered_columns: list[str] = []
    mappings_by_page: list[list[dict[str, Any]]] = []

    for page in normalized_pages:
        headers = [
            str(value).strip()
            for value in (page.get("headers") or [])
            if str(value).strip()
        ]
        mappings = [
            _row_mapping(row, headers)
            for row in (page.get("rows") or [])
        ]
        mappings_by_page.append(mappings)

        for name in headers:
            if name != "頁碼" and name not in discovered_columns:
                discovered_columns.append(name)
        for row in mappings:
            for name in row:
                if name != "頁碼" and name not in discovered_columns:
                    discovered_columns.append(name)

    discovered = set(discovered_columns)
    is_cmm = all(
        any(alias in discovered for alias in aliases)
        for aliases in (
            ("实际值", "實際值"),
            ("名义值", "名義值"),
            ("上公差",),
            ("下公差",),
            ("偏差",),
        )
    )

    if is_cmm:
        columns = CMM_COLUMNS[:]
        rows: list[list[str]] = []
        for page_index, mappings in enumerate(mappings_by_page, start=1):
            page_number = normalized_pages[page_index - 1].get("page_number") or page_index
            for mapping in mappings:
                rows.append([
                    str(page_number),
                    _cell(_find_value(mapping, "項目")),
                    _cell(_find_value(mapping, "实际值")),
                    _cell(_find_value(mapping, "名义值")),
                    _cell(_find_value(mapping, "上公差")),
                    _cell(_find_value(mapping, "下公差")),
                    _cell(_find_value(mapping, "偏差")),
                ])
    else:
        columns = ["頁碼", *discovered_columns]
        if len(columns) == 1:
            columns.extend(["項目", "內容"])
        rows = []
        for page_index, mappings in enumerate(mappings_by_page, start=1):
            page_number = normalized_pages[page_index - 1].get("page_number") or page_index
            for mapping in mappings:
                rows.append([
                    str(page_number),
                    *[_cell(mapping.get(column)) for column in columns[1:]],
                ])

    return {
        "file_name": Path(file_name).name,
        "metadata": metadata or {},
        "columns": columns,
        "rows": rows,
    }
