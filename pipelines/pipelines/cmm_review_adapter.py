"""將 Aniki.py 的 CMM all_pages_result.json 轉為現有人工修正 API 所需資料。

此為不修改既有 OCR Agent 的獨立轉接器；需由 FastAPI /process 成功解析後呼叫。
不包含資料庫寫入，不會自行建立 review task。
"""
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


def _display(value: Any) -> Any:
    """Keep marker/value data losslessly; represent a verified empty as None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    if text.lower() in {"unknown", "unclear"}:
        return text.lower()
    return text


def make_cmm_review_payload(
    final_output: dict[str, Any],
    file_name: str,
) -> dict[str, Any]:
    """接受 Aniki 的完整輸出，回傳 /api/reviews 所需欄位。

    CMM 第一頁及續頁的 rows 會合併成一張可編輯表，另設「頁碼」欄位，
    避免原先多頁文件合併後無法追蹤誤辨位置。
    """
    pages = final_output.get("pages") or []
    if not isinstance(pages, list):
        raise ValueError("final_output.pages 必須為 list")

    table_rows: list[list[str]] = []
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict):
            raise ValueError(f"第 {index} 頁不是合法資料物件")

        page_number = page.get("page_number") or index
        for row in (page.get("rows") or []):
            if not isinstance(row, dict):
                raise ValueError(f"第 {page_number} 頁存在非 dict row")
            table_rows.append([
                str(page_number),
                _display(row.get("項目")),
                _display(row.get("实际值", row.get("實際值"))),
                _display(row.get("名义值", row.get("名義值"))),
                _display(row.get("上公差")),
                _display(row.get("下公差")),
                _display(row.get("偏差")),
            ])

    return {
        "file_name": Path(file_name).name,
        "metadata": final_output.get("metadata") or {},
        "columns": CMM_COLUMNS[:],
        "rows": table_rows,
    }


if __name__ == "__main__":
    sample = {
        "metadata": {"品號": "700923"},
        "pages": [
            {"page_number": 1, "rows": [
                {"項目": "直径_圆1", "实际值": "31.7874", "名义值": "31.7880", "上公差": "0.0120", "下公差": "-0.0120", "偏差": "-0.0006"}
            ]},
            {"page_number": 2, "rows": [
                {"項目": "圆度_圆2", "实际值": "0.0043", "名义值": "0.0000", "上公差": "0.0120", "下公差": None, "偏差": "0.0043"}
            ]},
        ],
    }
    payload = make_cmm_review_payload(sample, "CMM.pdf")
    assert len(payload["rows"]) == 2
    assert payload["rows"][1][0] == "2"
    assert payload["rows"][1][5] == ""
    print("CMM review payload adapter test: PASS")


# Generic uses each page's own headers, all pages merged into a single editable task.
def make_generic_review_payload(final_output: dict[str, Any], file_name: str) -> dict[str, Any]:
    pages=final_output.get("pages") or []
    columns=["頁碼"]
    for page in pages:
        for col in (page.get("headers") or []):
            col=str(col)
            if col not in columns:
                columns.append(col)
        for row in (page.get("rows") or []):
            if isinstance(row,dict):
                for col in row:
                    if col not in columns: columns.append(col)
    result=[]
    for idx,page in enumerate(pages,start=1):
        page_no=page.get("page_number") or idx
        headers=page.get("headers") or []
        for row in (page.get("rows") or []):
            if isinstance(row,dict):
                result.append([str(page_no)]+[_display(row.get(c)) for c in columns[1:]])
            elif isinstance(row,list):
                mapping=dict(zip(headers,row))
                result.append([str(page_no)]+[_display(mapping.get(c)) for c in columns[1:]])
    if not result:
        raise ValueError("通用檢查表未擷取到任何資料列，請檢查 OCR 錯誤")
    return {"file_name":Path(file_name).name,"metadata":final_output.get("metadata") or {},
            "columns":columns,"rows":result}
