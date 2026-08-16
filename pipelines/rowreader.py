import base64
import json
import os
import re
from typing import Dict, Any, List

import requests

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf


# =========================
# 1. 基本設定
# =========================

OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://ollama:11434/api/chat",
).strip()
MODEL_NAME = os.getenv(
    "VLM_MODEL",
    "qwen2.5vl:7b",
).strip()
PDF_DPI = 150

REQUIRED_KEYS = ["項目", "实际值", "名义值", "上公差", "下公差", "偏差"]


# =========================
# 2. 檔案轉 base64
# =========================

def file_to_base64_image(file_path: str, page_number: int = 1) -> str:
    """
    支援 PDF / 圖片。
    PDF 會先轉成 PNG，再轉成 base64。
    """

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"找不到檔案：{file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        doc = pymupdf.open(file_path)

        if page_number < 1 or page_number > len(doc):
            total_pages = len(doc)
            doc.close()
            raise ValueError(f"PDF 只有 {total_pages} 頁，但你輸入第 {page_number} 頁")

        page = doc[page_number - 1]
        pix = page.get_pixmap(dpi=PDF_DPI)
        png_bytes = pix.tobytes("png")

        doc.close()

        return base64.b64encode(png_bytes).decode("utf-8")

    elif ext in [".png", ".jpg", ".jpeg", ".webp"]:
        with open(file_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    else:
        raise ValueError(f"不支援的檔案格式：{ext}，請使用 PDF 或圖片")


# =========================
# 3. 呼叫 Ollama VLM
# =========================

def call_vlm_with_file(prompt: str, file_path: str, page_number: int = 1) -> str:
    """
    傳 prompt + PDF/圖片 給 Ollama VLM。
    這版不要求模型輸出 JSON，而是輸出 pipe-separated table。
    """

    image_base64 = file_to_base64_image(file_path, page_number)

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [image_base64],
            }
        ],
        "stream": False,
        "keep_alive": "0s",
        "options": {
            "temperature": 0,
            "num_predict": 4096,
            "num_ctx": 8192
        }
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=(30, 1800),
    )

    if response.status_code != 200:
        print("\n========== Row Agent Ollama 錯誤內容 ==========")
        print(response.text)
        print("===============================================\n")

    response.raise_for_status()

    result = response.json()
    return result["message"]["content"]


# =========================
# 4. 清理模型文字
# =========================

def clean_text_output(text: str) -> str:
    """
    清掉模型可能加上的 markdown code block。
    """

    text = text.strip()

    text = re.sub(r"```csv\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```text\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*", "", text)

    return text.strip()


# =========================
# 5. 解析 pipe table
# =========================

def parse_rows_pipe_table(text: str) -> List[Dict[str, Any]]:
    """
    預期模型回傳：

    項目|实际值|名义值|上公差|下公差|偏差
    直径_圆1|55.0131|55.0000|0.0200|0.0000|0.0131
    直径_圆8|5.7417|5.7400|0.0100|-0.0100|0.0017

    解析後轉成：

    [
      {
        "項目": "直径_圆1",
        "实际值": "55.0131",
        ...
      }
    ]
    """

    text = clean_text_output(text)

    rows: List[Dict[str, Any]] = []

    lines = text.splitlines()

    for line in lines:
        line = line.strip()

        if not line:
            continue

        # 跳過 markdown 表格分隔線
        if set(line.replace("|", "").replace("-", "").replace(" ", "")) == set():
            continue

        # 如果模型輸出成 markdown table：| A | B |
        if line.startswith("|"):
            line = line[1:]

        if line.endswith("|"):
            line = line[:-1]

        # 沒有 pipe 的行不要
        if "|" not in line:
            continue

        parts = [part.strip() for part in line.split("|")]

        # 只接受剛好 6 欄
        if len(parts) != 6:
            continue

        item, actual, nominal, upper_tol, lower_tol, deviation = parts

        # 跳過表頭
        if item in ["項目", "项目", "項目名稱", "项目名称", "特性名稱", "特性名称"]:
            continue

        # 跳過空項目
        if not item:
            continue

        # 跳過 metadata / summary
        banned_items = [
            "程序名",
            "操作者",
            "时间",
            "日期",
            "订单号",
            "图号",
            "测量程序",
            "所有结果",
            "所有特性",
            "公差内",
            "超出公差",
            "超过警告线",
            "未计算",
            "全部坐标系",
            "全部文本元素",
        ]

        if any(bad in item for bad in banned_items):
            continue

        lower_tol_value: Any = lower_tol

        if lower_tol in ["null", "NULL", "None", "none", ""]:
            lower_tol_value = None

        row = {
            "項目": item,
            "实际值": actual if actual not in ["null", "NULL", "None", "none", ""] else None,
            "名义值": nominal if nominal not in ["null", "NULL", "None", "none", ""] else None,
            "上公差": upper_tol if upper_tol not in ["null", "NULL", "None", "none", ""] else None,
            "下公差": lower_tol_value,
            "偏差": deviation if deviation not in ["null", "NULL", "None", "none", ""] else None,
        }

        rows.append(row)

    return rows


# =========================
# 6. 建立 Row Reader Prompt
# =========================

def build_rows_prompt(headers_json: str) -> str:
    prompt = f"""
你是一個表格資料列讀取 agent。

請查看圖片中的「主要資料表」，根據以下 headers 讀取資料列：

{headers_json}

你的任務：
只讀主要資料表中的資料列。

重要：
這次不要輸出 JSON。
這次不要輸出 JSON。
這次不要輸出 JSON。

你要輸出純文字表格，每一列一筆資料。

固定欄位只有以下 6 個，順序不能改：

項目|实际值|名义值|上公差|下公差|偏差

========================
讀取規則
========================

1. 只讀主要資料表。

2. 不要讀頁首 metadata，例如：
   程序名、操作者、时间、日期、订单号、图号、测量程序、CMM、增加的工件号。

3. 不要讀摘要區，例如：
   所有结果、所有特性、公差内、超出公差、超过警告线、未计算、全部坐标系、全部文本元素。

4. 每一列的第一欄「項目」必須讀取表格最左邊的項目名稱。

5. 項目名稱可能像：
   直径_圆1
   直径_圆2
   直径_圆3
   直径_圆4
   直径_圆5
   直径_圆6
   直径_圆7
   直径_圆8
   直径_圆9
   直径_圆10
   圆度_圆1
   圆度_圆2
   同心度1
   同心度2
   同心度3
   同轴度1
   平行度1
   平行度2
   平行度3

6. 不要重複上一列的項目名稱。
   如果第二列是直径_圆10，就不能輸出成直径_圆9。

7. 每一列都必須剛好有 6 欄。

8. 每一列必須使用 | 分隔欄位。

9. 每一列格式必須是：

   項目名稱|实际值|名义值|上公差|下公差|偏差

10. 數字、符號、小數位數都要照圖片原樣抄寫。
    例如：
    48.5000 不要改成 48.5
    0.0100 不要改成 0.01

11. 禁止自行計算任何欄位。

12. 「偏差」必須讀取最右邊「偏差」欄位的值。
    不可以用 actual - nominal。
    不可以用 actual - upper tolerance。
    不可以用任何公式自行計算。

13. 如果某格真的空白，請填 null。
    如果他是0.000，不要填null，要填0.000
14. 如果某格看不清楚，請填 unclear。

15. 不要 markdown。

16. 不要解釋。

17. 不要加 ```。

18. 不要輸出 JSON。

========================
輸出格式範例
========================

項目|实际值|名义值|上公差|下公差|偏差
直径_圆1|55.0131|55.0000|0.0200|0.0000|0.0131
直径_圆8|5.7417|5.7400|0.0100|-0.0100|0.0017
圆度_圆1|0.0022|0.0000|0.0100|null|0.0022

請只輸出上述格式的純文字表格。
重要：
「項目」欄位必須完整保留圖片最左邊的項目名稱。
如果項目名稱前面有數字、點號、英文代號，必須一起保留。

例如：

正確：
32.迪卡尔距离2
22.Y-值_对称平面1
31.X-值_对称平面2
36.Y-值_对称点3
26.迪卡尔距离3
24.迪卡尔距离5

錯誤：
迪卡尔距离2
Y-值_对称平面1
X-值_对称平面2
Y-值_对称点3
迪卡尔距离3
迪卡尔距离5

不要省略項目前面的數字、點號或英文代號。
"""
    return prompt


# =========================
# 7. Agent 2：讀 rows
# =========================

def read_rows_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Agent 2：
    根據 Agent 1 讀到的 headers，讀取主要資料表下面每一列資料。

    這版做法：
    1. 叫 VLM 輸出 pipe table
    2. Python 把 pipe table 轉成 rows dict
    """

    file_path = state["file_path"]
    page_number = state["page_number"]
    headers = state.get("headers", [])

    if not headers:
        errors = state.get("errors", [])
        errors.append("read_rows_agent error: 沒有 headers，所以無法讀取 rows。")

        return {
            **state,
            "rows": [],
            "row_raw_response": "",
            "errors": errors,
        }

    headers_json = json.dumps(headers, ensure_ascii=False)
    prompt = build_rows_prompt(headers_json)

    try:
        last_raw_response = ""

        # 最多重試 3 次
        for attempt in range(3):
            raw_response = call_vlm_with_file(prompt, file_path, page_number)
            last_raw_response = raw_response

            rows = parse_rows_pipe_table(raw_response)

            if rows:
                return {
                    **state,
                    "rows": rows,
                    "row_raw_response": raw_response,
                }

        errors = state.get("errors", [])
        errors.append(
            "read_rows_agent error: Row Agent 沒有成功讀到 rows。"
        )

        return {
            **state,
            "rows": [],
            "row_raw_response": last_raw_response,
            "errors": errors,
        }

    except Exception as error:
        errors = state.get("errors", [])
        errors.append(f"read_rows_agent error: {error}")

        return {
            **state,
            "rows": [],
            "row_raw_response": "",
            "errors": errors,
        }
