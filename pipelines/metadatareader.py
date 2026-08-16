import base64
import json
import os
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

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

METADATA_KEYS = ["品號", "製程", "機台", "流水號"]


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
    Metadata 通常在第 1 頁最上方，所以預設讀第 1 頁。
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
            "num_predict": 2048,
            "num_ctx": 8192,
        },
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=(30, 600),
    )

    if response.status_code != 200:
        print("\n========== Metadata Agent Ollama 錯誤內容 ==========")
        print(response.text)
        print("=================================================\n")

    response.raise_for_status()

    result = response.json()
    return result["message"]["content"]


# =========================
# 4. 清理 / 解析 JSON
# =========================

def clean_json_text(text: str) -> str:
    """
    清掉模型可能加上的 markdown，只保留 JSON。
    """

    text = text.strip()
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*", "", text)

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()


def normalize_empty(value: Any) -> Optional[str]:
    """
    把空字串 / null 字樣統一成 None。
    其他值轉成去頭尾空白的字串。
    """

    if value is None:
        return None

    value = str(value).strip()

    if value in ["", "null", "NULL", "None", "none", "unclear"]:
        return None

    return value


def split_program_name(program_name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    從程序名 / 測量程序中嘗試拆出：品號、製程。

    例：
    20-1938A-3D铣1-2024.07.05 -> 品號=20-1938A, 製程=3D铣1
    600-202-車2 -> 品號=600-202, 製程=車2
    """

    if not program_name:
        return None, None

    text = str(program_name).strip()

    # 移除最後面的日期，例如 -2024.07.05 / _2024-07-05
    text = re.sub(r"[-_]\d{4}[./-]\d{1,2}[./-]\d{1,2}$", "", text)

    parts = [part.strip() for part in re.split(r"[-_]", text) if part.strip()]

    if len(parts) < 2:
        return text or None, None

    process_keywords = [
        "车", "車", "铣", "銑", "磨", "钻", "鑽",
        "线割", "線割", "放电", "放電",
        "CNC", "cnc", "D",
    ]

    process_index = None

    # 從右邊找最像製程的片段
    for index in range(len(parts) - 1, 0, -1):
        part = parts[index]
        if any(keyword in part for keyword in process_keywords):
            process_index = index
            break

    # 找不到就退而求其次：最後一段當製程
    if process_index is None:
        process_index = len(parts) - 1

    item_no = "-".join(parts[:process_index]).strip()
    process = "-".join(parts[process_index:]).strip()

    return item_no or None, process or None


def parse_metadata_json(text: str) -> Dict[str, Any]:
    """
    解析 Metadata Agent 回覆，並保證至少有：
    品號、製程、機台、流水號。
    """

    cleaned = clean_json_text(text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError(f"Metadata Agent 回覆不是合法 JSON：\n{cleaned}") from error

    if not isinstance(data, dict):
        data = {}

    aliases = {
        "品號": ["品號", "品号", "圖號", "图号", "drawingno", "drawing_no"],
        "製程": ["製程", "制程", "製造流程", "process"],
        "機台": ["機台", "机台", "CMM", "cmm", "設備", "设备"],
        "流水號": [
            "流水號", "流水号",
            "工件號", "工件号",
            "增加的工件號", "增加的工件号",
            "workpiece_no",
        ],
    }

    metadata: Dict[str, Any] = {}

    for target_key, possible_keys in aliases.items():
        value = None
        for key in possible_keys:
            if key in data:
                value = normalize_empty(data.get(key))
                if value is not None:
                    break
        metadata[target_key] = value

    extra_keys = [
        "程序名",
        "測量程序",
        "测量程序",
        "操作者",
        "時間",
        "时间",
        "日期",
        "訂單號",
        "订单号",
        "圖號",
        "图号",
    ]

    for key in extra_keys:
        if key in data:
            metadata[key] = normalize_empty(data.get(key))

    # 如果品號 / 製程沒讀好，就從程序名 / 測量程序補拆
    program_name = (
        normalize_empty(data.get("程序名"))
        or normalize_empty(data.get("測量程序"))
        or normalize_empty(data.get("测量程序"))
    )

    if program_name:
        item_no, process = split_program_name(program_name)
        metadata.setdefault("程序名", program_name)

        if not metadata.get("品號"):
            metadata["品號"] = item_no

        if not metadata.get("製程"):
            metadata["製程"] = process

    for key in METADATA_KEYS:
        metadata.setdefault(key, None)

    return metadata


# =========================
# 5. Metadata Agent Prompt
# =========================

def build_metadata_prompt() -> str:
    return """
你是一個 PDF 頁首 metadata 讀取 agent。

你的任務只有一個：
讀取圖片最上方的工件基本資料，不要讀主要測量表格的 rows。

請優先讀這些欄位：
1. 測量程序 / 程序名
2. CMM
3. 增加的工件号 / 增加的工件號
4. 操作者
5. 时间 / 時間
6. 日期
7. 图号 / 圖號
8. 订单号 / 訂單號

你最後一定要輸出以下資料：
- 品號
- 製程
- 機台
- 流水號

判斷規則：
1. 「機台」通常來自 CMM 欄位。
2. 「流水號」通常來自「增加的工件号」或「增加的工件號」。
3. 「品號」和「製程」通常來自「測量程序」或「程序名」。
4. 程序名可能長得像：20-1938A-3D铣1-2024.07.05
   這時：
   品號 = 20-1938A
   製程 = 3D铣1
5. 品號本身可能有短橫線，例如：600-202。
   所以不要只用第一個短橫線切開。
6. 如果看到：600-202-車2
   這時：
   品號 = 600-202
   製程 = 車2
7. 不要把「实际值、名义值、上公差、下公差、偏差」當成 metadata。
8. 不要讀「所有结果、所有特性、公差内、超出公差」這種摘要。
9. 看不到的欄位填 null。
10. 不要自己發明資料。

請只輸出 JSON。
不要解釋。
不要 markdown。
不要加 ```。

輸出格式必須完全長這樣：

{
  "品號": "20-1938A",
  "製程": "3D铣1",
  "機台": "C32Bit",
  "流水號": "35",
  "程序名": "20-1938A-3D铣1-2024.07.05",
  "操作者": "Master",
  "時間": "09时25分36秒",
  "日期": "2026年6月01日",
  "圖號": "* drawingno *",
  "訂單號": "* order *"
}
"""


# =========================
# 6. Agent 0：讀 metadata
# =========================

def read_metadata_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Agent 0：
    只負責讀頁首 metadata。

    metadata 通常只需要讀第 1 頁一次。
    如果 state 裡已經有 metadata，就直接沿用，不重複呼叫 VLM。
    """

    existing_metadata = state.get("metadata") or {}

    if all(existing_metadata.get(key) for key in METADATA_KEYS):
        return state

    file_path = state["file_path"]

    try:
        prompt = build_metadata_prompt()

        # 固定讀第 1 頁，因為完整 metadata 通常在第一頁最上方
        raw_response = call_vlm_with_file(prompt, file_path, page_number=1)
        metadata = parse_metadata_json(raw_response)

        return {
            **state,
            "metadata": metadata,
            "metadata_raw_response": raw_response,
        }

    except Exception as error:
        errors = state.get("errors", [])
        errors.append(f"read_metadata_agent error: {error}")

        return {
            **state,
            "metadata": existing_metadata,
            "metadata_raw_response": "",
            "errors": errors,
        }


# =========================
# 7. 檔案資料，不靠 VLM，直接由程式產生
# =========================

def build_file_info(
    file_path: str,
    upload_time: Optional[str] = None,
    file_content: Optional[str] = None,
) -> Dict[str, Any]:
    """
    對應資料表：
    檔案(檔名, 路徑, 上傳時間, 檔案內容)

    檔名、路徑、上傳時間不是 PDF 內文，建議由程式產生。
    """

    if upload_time is None:
        upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "檔名": os.path.basename(file_path),
        "路徑": os.path.abspath(file_path),
        "上傳時間": upload_time,
        "檔案內容": file_content,
    }
