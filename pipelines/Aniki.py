import base64
import json
import os
import re
import sys
from typing import TypedDict, List, Dict, Any

import requests
from langgraph.graph import StateGraph, START, END
from neon_db import save_aniki_result
# 匯入另一份 py 檔案裡的第二個 agent
from rowreader import read_rows_agent
from generic_rowreader import read_generic_page
from metadatareader import read_metadata_agent, build_file_info
# PyMuPDF：用來把 PDF 頁面轉成圖片
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

# 你有 qwen2.5vl:7b，讀表格建議先用 7b
# 如果你想用 3b，也可以改成 "qwen2.5vl:3b"
MODEL_NAME = os.getenv(
    "VLM_MODEL",
    "qwen2.5vl:7b",
).strip()

# PDF 轉圖片清晰度
# 如果圖片太大報錯，可以改 120
# 如果讀不清楚，可以改 180 或 200
PDF_DPI = 150
# Aniki.py 所在的資料夾
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =========================
# 2. LangGraph State
# =========================

class TableState(TypedDict):
    file_path: str
    page_number: int

    metadata: Dict[str, Any]
    headers: List[str]
    rows: List[Dict[str, Any]]

    metadata_raw_response: str
    header_raw_response: str
    row_raw_response: str

    errors: List[str]


# =========================
# 3. 檔案轉 base64
# =========================

def file_to_base64_image(file_path: str, page_number: int = 1) -> str:
    """
    支援圖片與 PDF。

    如果是圖片：
        直接轉 base64。

    如果是 PDF：
        先把指定頁轉成 PNG，再轉 base64。
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
# 4. 清理 VLM 回傳 JSON
# =========================

def clean_json_text(text: str) -> str:
    """
    VLM 有時候會回傳：

    ```json
    {
      "headers": ["姓名", "年齡"]
    }
    ```

    這個函式會清掉 markdown，只留下 JSON。
    """

    text = text.strip()

    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*", "", text)

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text.strip()


def parse_headers_json(text: str) -> List[str]:
    """
    從 VLM 回覆裡面抓出 headers。
    並且強制第一欄一定是「項目」。
    """

    cleaned = clean_json_text(text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ValueError(f"Header Agent 回覆不是合法 JSON：\n{cleaned}") from error

    headers = data.get("headers", [])

    if not isinstance(headers, list):
        headers = []

    headers = [
        str(header).strip() if header is not None else "unclear"
        for header in headers
    ]

    # =========================
    # 先統一欄位名稱
    # =========================
    normalized_headers = []

    for header in headers:
        h = str(header).strip()

        if h in ["項目", "项目", "項目名稱", "项目名称", "特性名稱", "特性名称"]:
            h = "項目"

        normalized_headers.append(h)

    # =========================
    # 移除重複欄位
    # =========================
    deduped_headers = []

    for h in normalized_headers:
        if h not in deduped_headers:
            deduped_headers.append(h)

    headers = deduped_headers

    # =========================
    # 強制第一欄一定是「項目」
    # =========================
    if "項目" in headers:
        headers.remove("項目")

    headers.insert(0, "項目")

    return headers


# =========================
# 5. 呼叫 Ollama VLM
# =========================

def call_vlm_with_file(prompt: str, file_path: str, page_number: int = 1) -> str:
    """
    傳 prompt + PDF/圖片 給 Ollama VLM。
    如果是 PDF，會先把指定頁轉成圖片。
    每次都重新讀取圖片，不使用前一次結果。
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

        # 跑完後不要讓模型一直常駐
        "keep_alive": "0s",
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=(30, 600),
    )

    if response.status_code != 200:
        print("\n========== Header Agent Ollama 錯誤內容 ==========")
        print(response.text)
        print("===============================================\n")

    response.raise_for_status()

    result = response.json()
    return result["message"]["content"]


# =========================
# 6. Agent 1：讀表頭
# =========================

def read_headers_agent(state: TableState) -> TableState:
    """
    Agent 1：
    只負責讀取 PDF 或圖片中的主要資料表 headers。
    不讀 rows。
    """

    file_path = state["file_path"]
    page_number = state["page_number"]

    prompt = """
你是一個表格表頭辨識 agent。

請仔細查看圖片中的所有表格區塊。

你的任務只有一個：
找出「主要資料表」的欄位標題 headers。

重要判斷規則：
pro.每一個header第一項一定是"項目名稱"，第二的才是其他的
1. 每一個檔案都會有表格，請優先尋找主要資料表。

2. 不要把頁首 metadata 當成主要資料表。
   例如：
   程序名、操作者、时间、日期、订单号、图号、测量程序、CMM、增加的工件号。

3. 不要把摘要區當成主要資料表。
   例如：
   所有结果、所有特性、公差内、超出公差、超过警告线、未计算、全部坐标系、全部文本元素。

4. 主要資料表通常有多列重複資料。
   每一列通常會有一個項目名稱，後面接數值欄位。

5. 如果你看到類似以下欄位：
   实际值、名义值、上公差、下公差、偏差
   這些就是主要資料表的欄位標題。

6. 如果主要資料表最左邊有一欄是每列的項目名稱，例如：
   直径_圆1、直径_圆2、直径_圆9、直径_圆10、
   圆度_圆1、圆度_圆2、
   同心度1、同心度2、同心度3、
   同轴度1、
   平行度1、平行度2、平行度3
   但圖片中沒有明確表頭，請自動補一個 header：
   "特性名稱"

7. 如果補了 "特性名稱"，它必須放在 headers 的第一個位置。

8. headers 必須按照主要資料表從左到右的順序。

9. 只讀欄位標題，不要讀資料列。

10. 不要輸出 rows。

11. 如果某個欄位標題看不清楚，填 "unclear"。

12. 只有在圖片中完全沒有主要資料表時，才輸出空 headers。

請只輸出 JSON。
不要解釋。
不要加 markdown。

輸出格式必須完全長這樣：

{
  "headers": ["欄位1", "欄位2", "欄位3"]
}
"""

    try:
        raw_response = call_vlm_with_file(prompt, file_path, page_number)

        headers = parse_headers_json(raw_response)

        return {
            **state,
            "headers": headers,
            "header_raw_response": raw_response,
        }

    except Exception as error:
        errors = state.get("errors", [])
        errors.append(f"read_headers_agent error: {error}")

        return {
            **state,
            "headers": [],
            "header_raw_response": "",
            "errors": errors,
        }

    except Exception as error:
        errors = state.get("errors", [])
        errors.append(f"read_headers_agent error: {error}")

        return {
            **state,
            "headers": [],
            "header_raw_response": "",
            "errors": errors,
        }

# =========================
# 7. 建立 LangGraph
# =========================

def build_graph():
    """
    建立 LangGraph。

    流程：

    START
      ↓
    read_metadata_agent
      ↓
    read_headers_agent
      ↓
    read_rows_agent
      ↓
    END
    """

    builder = StateGraph(TableState)

    builder.add_node("read_metadata", read_metadata_agent)
    builder.add_node("read_headers", read_headers_agent)
    builder.add_node("read_rows", read_rows_agent)

    builder.add_edge(START, "read_metadata")
    builder.add_edge("read_metadata", "read_headers")
    builder.add_edge("read_headers", "read_rows")
    builder.add_edge("read_rows", END)

    return builder.compile()

def resolve_file_path(file_name: str) -> str:
    """
    使用者只需要提供檔案名稱，
    程式會自動到 Aniki.py 所在資料夾尋找檔案。
    """

    file_name = file_name.strip()

    if not file_name:
        raise ValueError("檔案名稱不能是空的")

    # 只允許傳入檔名，不允許傳完整路徑
    if os.path.basename(file_name) != file_name:
        raise ValueError("請只輸入檔案名稱，不要輸入完整路徑")

    file_path = os.path.join(BASE_DIR, file_name)

    if not os.path.isfile(file_path):
        raise FileNotFoundError(
            f"在資料夾中找不到檔案：{file_name}\n"
            f"搜尋位置：{BASE_DIR}"
        )

    return file_path
# =========================
# 8. 主程式
# =========================

def main():
    if len(sys.argv) < 2:
        print("請輸入 PDF 或圖片檔案名稱")
        print('用法：python Aniki.py "檔案.pdf" 1 2')
        return

    file_name = sys.argv[1]

    try:
        file_path = resolve_file_path(file_name)
    except (ValueError, FileNotFoundError) as error:
        print(error)
        return

    print(f"讀取檔案名稱：{file_name}")
    print(f"實際檔案位置：{file_path}")

    # 如果後面有輸入頁碼，就讀那些頁
    # 例如：python Aniki.py "xxx.pdf" 1 2
    if len(sys.argv) >= 3:
        page_numbers = [int(page) for page in sys.argv[2:]]
    else:
        page_numbers = [1]

    graph = build_graph()

    # 只在進入原本流程之前判斷一次文件格式。
    # True：執行原本 CMM 邏輯。
    # False：交給 generic_rowreader.py。
    detection_page = page_numbers[0]

    use_original_cmm_logic = detect_cmm_format(
        file_path=file_path,
        page_number=detection_page,
    )

    processing_mode = (
        "original_cmm"
        if use_original_cmm_logic
        else "generic"
    )

    print(f"文件處理模式：{processing_mode}")

    all_results = []
    metadata_cache: Dict[str, Any] = {}

    from datetime import datetime
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for page_number in page_numbers:
        print(f"\n\n==============================")
        print(f"開始讀第 {page_number} 頁")
        print(f"==============================")

        initial_state: TableState = {
            "file_path": file_path,
            "page_number": page_number,

            # 第 1 頁讀完後，後面頁面會沿用這份 metadata，避免重複讀取。
            "metadata": metadata_cache,
            "headers": [],
            "rows": [],

            "metadata_raw_response": "",
            "header_raw_response": "",
            "row_raw_response": "",

            "errors": [],
        }

        if use_original_cmm_logic:
            # 原本的 CMM 流程完全不動
            result = graph.invoke(initial_state)
        else:
            # 非固定 CMM 格式才走新的通用讀表邏輯
            result = read_generic_page(initial_state)

        # 把 metadata 存起來，讓第 2 頁、第 3 頁沿用
        if result.get("metadata"):
            metadata_cache = result["metadata"]

        all_results.append(result)

        print(f"\n========== 第 {page_number} 頁：Agent 0 讀到的 metadata ==========")
        print(json.dumps(result.get("metadata", {}), ensure_ascii=False, indent=2))

        print(f"\n========== 第 {page_number} 頁：Agent 1 讀到的 headers ==========")
        print(result["headers"])

        print(f"\n========== 第 {page_number} 頁：Agent 2 讀到的 rows ==========")
        print(json.dumps(result["rows"], ensure_ascii=False, indent=2))

        print(f"\n========== 第 {page_number} 頁：errors ==========")
        print(result.get("errors", []))

    # 先整理完整解析內容
    parsed_content = {
        "metadata": metadata_cache,
        "pages": all_results,
    }

    # 檔案資料：檔名、路徑、上傳時間、檔案內容
    file_info = build_file_info(
        file_path=file_path,
        upload_time=upload_time,
        file_content=json.dumps(parsed_content, ensure_ascii=False, indent=2),
    )

    final_output = {
    "file_info": file_info,
    "metadata": metadata_cache,
    "pages": all_results,
    }

    try:
        neon_counts = save_aniki_result(final_output)

        print("\n========== Neon 寫入成功 ==========")
        print(json.dumps(
            neon_counts,
            ensure_ascii=False,
            indent=2,
        ))

    except Exception as error:
        print("\n========== Neon 寫入失敗 ==========")
        print(type(error).__name__)
        print(error)

    with open("all_pages_result.json", "w", encoding="utf-8") as f:
        json.dump(final_output, f, ensure_ascii=False, indent=2)



    print("\n\n========== 全部頁面讀取完成 ==========")
    print("結果已存成 all_pages_result.json")

    #print("\n========== 檔案資料 file_info ==========")
    #print(json.dumps(file_info, ensure_ascii=False, indent=2))
# =========================
# 文件分類：由 Aniki.py 負責
# =========================

def normalize_router_text(text: str) -> str:
    return re.sub(
        r"[\s\r\n\t：:|_*]+",
        "",
        str(text or "").lower(),
    )


def extract_pdf_text(
    file_path: str,
    max_pages: int = 2,
) -> str:
    """
    讀取 PDF 前幾頁的原生文字層。
    掃描 PDF 沒有文字層時會回傳空字串。
    """

    ext = os.path.splitext(file_path)[1].lower()

    if ext != ".pdf":
        return ""

    document = pymupdf.open(file_path)

    try:
        texts = []

        page_count = min(
            len(document),
            max_pages,
        )

        for page_index in range(page_count):
            text = (
                document[page_index].get_text("text")
                or ""
            )
            texts.append(text)

        return "\n".join(texts)

    finally:
        document.close()


def detect_cmm_format(
    file_path: str,
    page_number: int = 1,
) -> bool:
    """
    True：
        使用原本 CMM 流程：
        metadatareader → header agent → rowreader

    False：
        使用 generic_rowreader
    """

    # ==========================================
    # 第一層：優先檢查 PDF 原生文字
    # ==========================================

    pdf_text = extract_pdf_text(
        file_path=file_path,
        max_pages=2,
    )

    normalized_text = normalize_router_text(
        pdf_text
    )

    required_groups = [
        ("实际值", "實際值"),
        ("名义值", "名義值"),
        ("上公差",),
        ("下公差",),
        ("偏差",),
    ]

    has_all_cmm_headers = all(
        any(
            normalize_router_text(alias)
            in normalized_text
            for alias in aliases
        )
        for aliases in required_groups
    )

    if has_all_cmm_headers:
        print(
            "[Aniki Router] 分類方式：PDF 原生文字層"
        )
        print(
            "[Aniki Router] 文件類型：CMM"
        )
        return True

    # ==========================================
    # 第二層：掃描件再交給 VLM 判斷
    # ==========================================

    prompt = """
你是文件分類器。

請判斷圖片中的文件類型。

類型一：cmm

CMM 文件通常具有以下特徵：
- ZEISS
- Calypso
- CMM
- 測量程序或程序名
- 實際值／实际值
- 名義值／名义值
- 上公差
- 下公差
- 偏差
- X值、Y值、Z值、直徑、圓度、位置度等量測項目

類型二：generic

一般檢查表通常具有以下特徵：
- 編號
- 量具編號
- 代號
- 刀號
- 檢驗標準
- 公差
- 首件
- 1～11 的檢查欄

只輸出合法 JSON，不要解釋：

{
  "document_type": "cmm"
}

document_type 只能是：
cmm
generic
"""

    try:
        raw_response = call_vlm_with_file(
            prompt=prompt,
            file_path=file_path,
            page_number=page_number,
        )

        cleaned = clean_json_text(
            raw_response
        )

        data = json.loads(cleaned)

        document_type = str(
            data.get("document_type")
            or ""
        ).strip().lower()

        is_cmm = (
            document_type == "cmm"
        )

        print(
            "[Aniki Router] 分類方式：VLM"
        )
        print(
            "[Aniki Router] 文件類型："
            f"{'CMM' if is_cmm else 'generic'}"
        )

        return is_cmm

    except Exception as error:
        print(
            "[Aniki Router] 分類失敗："
            f"{type(error).__name__}: {error}"
        )
        print(
            "[Aniki Router] 預設使用 generic"
        )

        return False
if __name__ == "__main__":
    main()
