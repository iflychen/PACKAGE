import base64
import io
import json
import os
import re
from typing import Any

import requests
from PIL import Image, ImageFilter, ImageOps

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf


OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://ollama:11434/api/chat",
).strip()
MODEL_NAME = os.getenv("VLM_MODEL", "qwen2.5vl:7b")
RECHECK_MODEL_NAME = os.getenv(
    "VLM_RECHECK_MODEL",
    MODEL_NAME,
)
OLLAMA_KEEP_ALIVE = os.getenv(
    "OLLAMA_KEEP_ALIVE",
    "10m",
)

DETECTION_DPI = 100
HEADER_DPI = 180
ROW_DPI = 320
OLLAMA_NUM_CTX = 8192
GENERIC_READER_VERSION = "2026-07-31-v10-best-effort-no-unclear"

# 寬表格裁切比例，可依其他表單微調
TABLE_TOP_RATIO = 0.20
TABLE_BOTTOM_RATIO = 0.95

_DOCUMENT_CACHE: dict[
    str,
    dict[str, Any],
] = {}


def clean_json_text(
    text: str,
) -> str:
    text = (text or "").strip()

    text = re.sub(
        r"```json\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"```\s*",
        "",
        text,
    )

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        return text[
            start:end + 1
        ].strip()

    start = text.find("[")
    end = text.rfind("]")

    if start != -1 and end > start:
        return text[
            start:end + 1
        ].strip()

    return text


def repair_model_json(
    text: str,
) -> str:
    """
    修復視覺模型常見的非標準 JSON。
    """
    text = re.sub(
        r"\\u0?51\.1",
        "ø5.1",
        text,
        flags=re.IGNORECASE,
    )

    # 裸的 unclear / clear 不是合法 JSON。
    text = re.sub(
        r'(:\s*)unclear(?=\s*[,}\]])',
        r'\1"unclear"',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'(:\s*)clear(?=\s*[,}\]])',
        r'\1"clear"',
        text,
        flags=re.IGNORECASE,
    )

    # Python 常見值改成 JSON。
    text = re.sub(
        r'(:\s*)None(?=\s*[,}\]])',
        r'\1null',
        text,
    )
    text = re.sub(
        r'(:\s*)True(?=\s*[,}\]])',
        r'\1true',
        text,
    )
    text = re.sub(
        r'(:\s*)False(?=\s*[,}\]])',
        r'\1false',
        text,
    )

    return re.sub(
        r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})',
        r"\\\\",
        text,
    )

def parse_json_response(
    raw: str,
    agent_name: str,
) -> Any:
    cleaned = repair_model_json(
        clean_json_text(raw)
    )

    try:
        return json.loads(cleaned)

    except json.JSONDecodeError as error:
        raise ValueError(
            f"{agent_name} 回覆不是合法 JSON：\n"
            f"{cleaned}"
        ) from error


def normalize_cell_value(
    value: Any,
) -> Any:
    if value is None:
        return None

    text = str(value).strip()

    if text.lower() in {
        "",
        "null",
        "none",
        "n/a",
        "na",
        "空白",
        "無",
        "没有",
        "沒有",
    }:
        return None

    return text


def normalize_key(
    value: Any,
) -> str:
    return re.sub(
        r"[\s\r\n\t：:]",
        "",
        str(value or "").strip(),
    ).lower()


def normalize_row_id(
    value: Any,
) -> str:
    return re.sub(
        r"[^0-9a-zA-Z一-龥_-]",
        "",
        normalize_key(value),
    )


def make_unique_headers(
    headers: list[Any],
) -> list[str]:
    output: list[str] = []
    counts: dict[str, int] = {}

    for raw_header in headers:
        if raw_header is None:
            continue

        header = str(
            raw_header
        ).strip()

        if not header:
            continue

        counts[header] = (
            counts.get(header, 0)
            + 1
        )

        if counts[header] == 1:
            output.append(header)
        else:
            output.append(
                f"{header}_{counts[header]}"
            )

    return output


def image_to_base64(
    image: Image.Image,
) -> str:
    buffer = io.BytesIO()

    image.convert(
        "RGB"
    ).save(
        buffer,
        format="PNG",
    )

    return base64.b64encode(
        buffer.getvalue()
    ).decode("utf-8")


def render_page_image(
    file_path: str,
    page_number: int,
    rotation: int,
    dpi: int,
) -> Image.Image:
    if not os.path.isfile(
        file_path
    ):
        raise FileNotFoundError(
            f"找不到檔案：{file_path}"
        )

    document = pymupdf.open(
        file_path
    )

    try:
        if not 1 <= page_number <= len(document):
            raise ValueError(
                f"檔案共有 {len(document)} 頁，"
                f"無法讀取第 {page_number} 頁"
            )

        page = document[
            page_number - 1
        ]

        matrix = pymupdf.Matrix(
            dpi / 72,
            dpi / 72,
        ).prerotate(
            rotation
        )

        pixmap = page.get_pixmap(
            matrix=matrix,
            alpha=False,
        )

        return Image.open(
            io.BytesIO(
                pixmap.tobytes("png")
            )
        ).convert("RGB")

    finally:
        document.close()


def render_page_base64(
    file_path: str,
    page_number: int,
    rotation: int,
    dpi: int,
) -> str:
    return image_to_base64(
        render_page_image(
            file_path,
            page_number,
            rotation,
            dpi,
        )
    )


def call_vlm(
    prompt: str,
    images: list[str],
    timeout_seconds: int = 1200,
    model_name: str | None = None,
) -> str:
    payload = {
        "model": model_name or MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": images,
            }
        ],
        "stream": False,
        # 單格辨識會連續呼叫很多次，不要每次都卸載模型。
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0,
            "seed": 42,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=(
            30,
            timeout_seconds,
        ),
    )

    if response.status_code != 200:
        print(
            "\n========== Generic Reader "
            "Ollama 錯誤 =========="
        )

        print(response.text)

        print(
            "================================"
            "================\n"
        )

    response.raise_for_status()

    return response.json()[
        "message"
    ][
        "content"
    ]


def detect_cmm_format(
    file_path: str,
    page_number: int = 1,
) -> bool:
    """
    只有同時包含：

    實際值
    名義值
    上公差
    下公差
    偏差

    才使用原本 CMM 邏輯。
    """

    cache_key = os.path.abspath(
        file_path
    )

    if cache_key in _DOCUMENT_CACHE:
        return bool(
            _DOCUMENT_CACHE[
                cache_key
            ][
                "is_cmm"
            ]
        )

    images = [
        render_page_base64(
            file_path,
            page_number,
            rotation,
            DETECTION_DPI,
        )
        for rotation in (
            0,
            90,
            270,
        )
    ]

    prompt = """
你是文件方向與表頭辨識器。

三張圖片依序是同一頁的：
第1張：原始 0 度
第2張：旋轉 90 度
第3張：旋轉 270 度

請只根據第1張原始圖片，找出公司大標題的位置。
公司大標題例如「○○工業股份有限公司」。

company_title_position 只能回答以下其中一個：
top
right
bottom
left

判斷規則：
公司大標題在圖片上方，回答 top。
公司大標題在圖片右側，回答 right。
公司大標題在圖片下方，回答 bottom。
公司大標題在圖片左側，回答 left。

另外，請從方向正立的圖片中，抄寫主要資料表由左到右的全部 headers。

真正的表頭通常包含：
編號、量具編號、代號、刀號、檢驗標準、公差、首件、1、2、3……11。

不要把「重要尺寸代號」的說明文字當成表頭。
不得新增欄位。
不得讀取資料列。

只輸出合法 JSON：

{
  "company_title_position": "right",
  "headers": [
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
    "11"
  ]
}
"""

    raw = call_vlm(
        prompt,
        images,
        timeout_seconds=600,
    )

    data = parse_json_response(
        raw,
        "格式判斷器",
    )

    if not isinstance(
        data,
        dict,
    ):
        data = {}

    company_position = str(
        data.get(
        "company_title_position",
        "top",
        )
    ).strip().lower()

    position_aliases = {
    "上": "top",
    "上方": "top",
    "右": "right",
    "右邊": "right",
    "右側": "right",
    "下": "bottom",
    "下方": "bottom",
    "左": "left",
    "左邊": "left",
    "左側": "left",
    }

    company_position = position_aliases.get(
        company_position,
        company_position,
    )

    rotation_map = {
        "top": 0,
        "right": 270,
        "bottom": 180,
        "left": 90,
    }

    rotation = rotation_map.get(
        company_position,
        0,
    )

    raw_headers = data.get(
        "headers",
        [],
    )

    if not isinstance(
        raw_headers,
        list,
    ):
        raw_headers = []


    replacements = {
        "实际值": "實際值",
        "实际値": "實際值",
        "名义值": "名義值",
        "名义値": "名義值",
    }

    normalized_headers: set[str] = set()

    for header in raw_headers:
        text = re.sub(
            r"[\s\r\n\t：:]",
            "",
            str(header).strip(),
        )

        if text:
            normalized_headers.add(
                replacements.get(
                    text,
                    text,
                )
            )

    required = {
        "實際值",
        "名義值",
        "上公差",
        "下公差",
        "偏差",
    }

    is_cmm = required.issubset(
        normalized_headers
    )

    _DOCUMENT_CACHE[
        cache_key
    ] = {
        "rotation": rotation,
        "is_cmm": is_cmm,
        "detected_headers": list(
            raw_headers
        ),
        "detection_raw_response": raw,
    }

    print(
        f"[Generic Router] "
        f"辨識到的表頭：{raw_headers}"
    )

    print(
        f"[Generic Router] "
        f"正規化表頭："
        f"{sorted(normalized_headers)}"
    )

    print(
        f"[Generic Router] "
        f"公司大標題位置：{company_position}"
    )

    print(
        f"[Generic Router] "
        f"文件方向：{rotation} 度"
    )

    print(
        f"[Generic Router] "
        f"是否使用原本 CMM 邏輯："
        f"{is_cmm}"
    )

    return is_cmm


def get_cached_rotation(
    file_path: str,
    page_number: int,
) -> int:
    key = os.path.abspath(
        file_path
    )

    if key not in _DOCUMENT_CACHE:
        detect_cmm_format(
            file_path,
            page_number,
        )

    return int(
        _DOCUMENT_CACHE[
            key
        ][
            "rotation"
        ]
    )


def get_router_headers(
    file_path: str,
) -> list[str]:
    raw = (
        _DOCUMENT_CACHE
        .get(
            os.path.abspath(
                file_path
            ),
            {},
        )
        .get(
            "detected_headers",
            [],
        )
    )

    if not isinstance(
        raw,
        list,
    ):
        return []

    return [
        str(header).strip()
        for header in raw
        if str(header).strip()
    ]


def merge_router_headers(
    headers: list[str],
    router_headers: list[str],
) -> list[str]:
    merged = list(headers)

    for raw_header in router_headers:
        header = str(
            raw_header
        ).strip()

        # Router 可能把「首件」和「1」
        # 黏成「首件1」
        match = re.fullmatch(
            r"首件\s*(\d+)",
            header,
        )

        if match:
            if "首件" not in merged:
                merged.append("首件")

            number = match.group(1)

            if number not in merged:
                merged.append(number)

        elif (
            header == "首件"
            or header.isdigit()
        ):
            if header not in merged:
                merged.append(header)

    return make_unique_headers(
        merged
    )


def complete_numbered_check_headers(
    headers: list[str],
) -> list[str]:
    """
    修正「首件」旁的第一個數字欄容易漏讀或黏在一起的問題。

    例如：
    首件、2、3、...、11
    會自動整理成：
    首件、1、2、3、...、11

    若模型回傳「首件1」，也會拆成「首件」與「1」。
    """

    expanded: list[str] = []

    for raw_header in headers:
        header = str(
            raw_header
        ).strip()

        if not header:
            continue

        match = re.fullmatch(
            r"首件\s*(\d+)",
            header,
        )

        if match:
            if "首件" not in expanded:
                expanded.append(
                    "首件"
                )

            number = match.group(1)

            if number not in expanded:
                expanded.append(
                    number
                )

            continue

        if header not in expanded:
            expanded.append(
                header
            )

    if "首件" not in expanded:
        return make_unique_headers(
            expanded
        )

    number_values = sorted(
        {
            int(header)
            for header in expanded
            if header.isdigit()
        }
    )

    if not number_values:
        return make_unique_headers(
            expanded
        )

    max_number = max(
        number_values
    )

    # 至少看到 2 之後，才視為連續編號欄，
    # 避免把其他普通數字表頭誤改。
    if max_number < 2:
        return make_unique_headers(
            expanded
        )

    static_headers = [
        header
        for header in expanded
        if (
            header != "首件"
            and not header.isdigit()
        )
    ]

    completed_headers = [
        *static_headers,
        "首件",
        *[
            str(number)
            for number in range(
                1,
                max_number + 1,
            )
        ],
    ]

    return make_unique_headers(
        completed_headers
    )


def parse_header_result(
    raw: str,
) -> tuple[
    dict[str, Any],
    list[str],
]:
    data = parse_json_response(
        raw,
        "Generic Header Agent",
    )

    if not isinstance(
        data,
        dict,
    ):
        data = {}

    metadata = data.get(
        "metadata",
        {},
    )

    headers = data.get(
        "headers",
        [],
    )

    if not isinstance(
        metadata,
        dict,
    ):
        metadata = {}

    if not isinstance(
        headers,
        list,
    ):
        headers = []

    cleaned_metadata = {
        str(key).strip(): (
            normalize_cell_value(value)
        )
        for key, value
        in metadata.items()
        if str(key).strip()
    }

    return (
        cleaned_metadata,
        make_unique_headers(
            headers
        ),
    )


def find_horizontal_grid_lines(
    image: Image.Image,
    dark_threshold: int = 170,
    min_dark_ratio: float = 0.20,
    merge_gap: int = 8,
) -> list[int]:
    """
    找出橫向表格線的位置。

    掃描線經過抗鋸齒或壓縮後，同一條粗格線可能被拆成
    相距數個像素的兩三條候選線，因此不能只合併相鄰 1 px。
    """
    gray = image.convert("L")
    width, height = gray.size
    raw_bytes = gray.tobytes()

    candidate_rows: list[int] = []

    for y in range(height):
        start = y * width
        row = raw_bytes[start:start + width]
        dark_count = sum(
            1
            for value in row
            if value < dark_threshold
        )

        if dark_count / max(width, 1) >= min_dark_ratio:
            candidate_rows.append(y)

    if not candidate_rows:
        return []

    groups: list[tuple[int, int]] = []
    group_start = candidate_rows[0]
    previous = candidate_rows[0]

    for y in candidate_rows[1:]:
        if y <= previous + merge_gap:
            previous = y
            continue

        groups.append((group_start, previous))
        group_start = y
        previous = y

    groups.append((group_start, previous))

    return [
        int(round((start + end) / 2))
        for start, end in groups
    ]


# ============================================================
# 固定格子辨識：右側不再使用三大區塊
# ============================================================

STANDARD_HEADERS = [
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

STANDARD_STATIC_HEADERS = STANDARD_HEADERS[:6]
STANDARD_CHECK_HEADERS = STANDARD_HEADERS[6:]

SAVE_CHECK_DEBUG_IMAGES = True
BLUE_MIN_PIXEL_COUNT = 35
BLUE_MIN_PIXEL_RATIO = 0.015


def normalize_metadata(
    metadata: dict[str, Any],
) -> dict[str, Any]:
    aliases = {
        "公司名称": "公司名稱",
        "客户": "客戶",
        "品名/图号": "品名/圖號",
        "品名圖號": "品名/圖號",
        "品名/圖號": "品名/圖號",
        "机台编号": "機台編號",
        "機台編號": "機台編號",
        "第二工程": "第二工程",
        "製程": "製程",
        "品號": "品號",
    }

    output: dict[str, Any] = {}

    for raw_key, raw_value in (
        metadata or {}
    ).items():
        compact_key = re.sub(
            r"[\s\r\n\t：:]",
            "",
            str(raw_key or ""),
        )

        key = aliases.get(
            compact_key,
            str(raw_key or "").strip(),
        )

        if not key:
            continue

        output[key] = normalize_cell_value(
            raw_value
        )

    part_number = (
        output.get("品號")
        or output.get("品名/圖號")
    )

    if part_number is not None:
        output["品號"] = part_number

    process_name = (
        output.get("製程")
        or output.get("第二工程")
    )

    if process_name is not None:
        output["製程"] = process_name

    return output


def clean_generic_headers(
    headers: list[str],
) -> list[str]:
    cleaned = [
        re.sub(
            r"[\s\r\n\t]",
            "",
            str(header or ""),
        )
        for header in headers
        if str(header or "").strip()
    ]

    # 模型可能把頁首欄位放在真正 Header 前面。
    if "編號" in cleaned:
        cleaned = cleaned[
            cleaned.index("編號"):
        ]

    cleaned = complete_numbered_check_headers(
        cleaned
    )

    normalized = {
        normalize_key(header)
        for header in cleaned
    }

    required = {
        normalize_key("編號"),
        normalize_key("量具編號"),
        normalize_key("檢驗標準"),
        normalize_key("公差"),
    }

    has_checks = (
        normalize_key("首件") in normalized
        or any(
            str(number) in cleaned
            for number in range(1, 12)
        )
    )

    if (
        required.issubset(normalized)
        and has_checks
    ):
        return list(STANDARD_HEADERS)

    return make_unique_headers(cleaned)


def crop_main_table(
    full_image: Image.Image,
) -> Image.Image:
    image = full_image.convert("RGB")
    width, height = image.size

    top = int(
        height * TABLE_TOP_RATIO
    )
    bottom = int(
        height * TABLE_BOTTOM_RATIO
    )

    return image.crop(
        (
            0,
            max(0, top),
            width,
            min(height, bottom),
        )
    )


def find_vertical_grid_lines(
    image: Image.Image,
    dark_threshold: int = 150,
    min_dark_ratio: float = 0.36,
) -> list[int]:
    gray = image.convert("L")
    width, height = gray.size
    raw = gray.tobytes()

    candidate_columns: list[int] = []

    for x in range(width):
        dark_count = 0

        for y in range(height):
            if raw[y * width + x] < dark_threshold:
                dark_count += 1

        if (
            dark_count / max(height, 1)
            >= min_dark_ratio
        ):
            candidate_columns.append(x)

    if not candidate_columns:
        return []

    groups: list[tuple[int, int]] = []
    start = candidate_columns[0]
    previous = start

    for x in candidate_columns[1:]:
        if x <= previous + 8:
            previous = x
            continue

        groups.append((start, previous))
        start = x
        previous = x

    groups.append((start, previous))

    return [
        int(round((start + end) / 2))
        for start, end in groups
    ]


def select_check_column_boundaries(
    vertical_lines: list[int],
    image_width: int,
) -> list[int]:
    """
    尋找最右側 12 個等寬檢查格：
    首件、1、2、...、11。
    """
    needed = len(
        STANDARD_CHECK_HEADERS
    ) + 1

    candidates: list[
        tuple[float, list[int]]
    ] = []

    for start in range(
        len(vertical_lines)
        - needed
        + 1
    ):
        sequence = vertical_lines[
            start:start + needed
        ]

        gaps = [
            sequence[index + 1]
            - sequence[index]
            for index in range(
                len(sequence) - 1
            )
        ]

        if not gaps:
            continue

        sorted_gaps = sorted(gaps)
        middle_gap = float(
            sorted_gaps[
                len(sorted_gaps) // 2
            ]
        )

        if (
            middle_gap < 8
            or middle_gap
            > image_width * 0.15
        ):
            continue

        deviation = sum(
            abs(gap - middle_gap)
            for gap in gaps
        ) / (
            len(gaps)
            * middle_gap
        )

        if deviation > 0.24:
            continue

        right_margin = (
            image_width - sequence[-1]
        ) / max(image_width, 1)

        # 右側檢查欄應靠近最右邊。
        score = (
            deviation * 10.0
            + right_margin * 3.0
        )

        candidates.append(
            (
                score,
                list(sequence),
            )
        )

    if not candidates:
        return []

    candidates.sort(
        key=lambda item: item[0]
    )

    return candidates[0][1]


def scale_cached_boundaries(
    ratios: list[float],
    length: int,
) -> list[int]:
    return [
        max(
            0,
            min(
                length - 1,
                int(round(ratio * length)),
            ),
        )
        for ratio in ratios
    ]


def crop_inner_cell(
    image: Image.Image,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> Image.Image:
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)

    # 手寫內容常貼近格線；原本 6% / 10% 容易切掉
    # 前導數字、小數點或勾號尾端。
    margin_x = max(
        1,
        int(width * 0.02),
    )
    margin_y = max(
        1,
        int(height * 0.04),
    )

    left = min(
        x1 - 1,
        x0 + margin_x,
    )
    right = max(
        left + 1,
        x1 - margin_x,
    )
    top = min(
        y1 - 1,
        y0 + margin_y,
    )
    bottom = max(
        top + 1,
        y1 - margin_y,
    )

    return image.crop(
        (
            left,
            top,
            right,
            bottom,
        )
    ).convert("RGB")


def is_blue_pixel(
    red: int,
    green: int,
    blue: int,
) -> bool:
    """
    掃描件中的藍筆通常 B 最大，
    且與 R/G 有足夠差距。
    """
    return (
        blue >= 75
        and blue >= red + 16
        and blue >= green + 5
        and (
            max(red, green, blue)
            - min(red, green, blue)
            >= 24
        )
    )


def blue_ink_count(
    image: Image.Image,
) -> int:
    count = 0

    for red, green, blue in (
        image.convert("RGB").getdata()
    ):
        if is_blue_pixel(
            red,
            green,
            blue,
        ):
            count += 1

    return count


def cell_has_blue_ink(
    image: Image.Image,
) -> bool:
    count = blue_ink_count(image)

    required = max(
        BLUE_MIN_PIXEL_COUNT,
        int(
            image.width
            * image.height
            * BLUE_MIN_PIXEL_RATIO
        ),
    )

    return count >= required


def get_blue_ink_bbox(
    image: Image.Image,
    padding_ratio: float = 0.16,
) -> tuple[int, int, int, int] | None:
    """
    找出藍色手寫內容的外框，並保留少量白邊。

    回傳座標使用 PIL crop 的：
    (left, top, right, bottom)
    """
    source = image.convert("RGB")
    pixels = source.load()

    xs: list[int] = []
    ys: list[int] = []

    for y in range(source.height):
        for x in range(source.width):
            if is_blue_pixel(
                *pixels[x, y]
            ):
                xs.append(x)
                ys.append(y)

    if not xs:
        return None

    left = min(xs)
    right = max(xs) + 1
    top = min(ys)
    bottom = max(ys) + 1

    padding_x = max(
        3,
        int(
            (right - left)
            * padding_ratio
        ),
    )
    padding_y = max(
        3,
        int(
            (bottom - top)
            * padding_ratio
        ),
    )

    return (
        max(0, left - padding_x),
        max(0, top - padding_y),
        min(source.width, right + padding_x),
        min(source.height, bottom + padding_y),
    )


def fit_image_to_canvas(
    image: Image.Image,
    canvas_size: tuple[int, int] = (
        1024,
        512,
    ),
    margin: int = 24,
    resample: Image.Resampling = (
        Image.Resampling.LANCZOS
    ),
) -> Image.Image:
    """
    等比例放大到固定白色畫布，不把數字硬拉寬或拉高。
    """
    source = image.convert("RGB")

    available_width = max(
        1,
        canvas_size[0] - 2 * margin,
    )
    available_height = max(
        1,
        canvas_size[1] - 2 * margin,
    )

    scale = min(
        available_width / max(1, source.width),
        available_height / max(1, source.height),
    )

    target_width = max(
        1,
        int(round(source.width * scale)),
    )
    target_height = max(
        1,
        int(round(source.height * scale)),
    )

    enlarged = source.resize(
        (
            target_width,
            target_height,
        ),
        resample,
    )

    canvas = Image.new(
        "RGB",
        canvas_size,
        "white",
    )

    canvas.paste(
        enlarged,
        (
            (
                canvas_size[0]
                - target_width
            ) // 2,
            (
                canvas_size[1]
                - target_height
            ) // 2,
        ),
    )

    enlarged.close()
    return canvas


def make_blue_only_image(
    image: Image.Image,
) -> Image.Image:
    """
    非藍色內容改成白色；藍筆保留成深藍色。

    不使用純黑硬遮罩，避免小數點與細筆畫被吃掉，
    也避免筆畫膨脹得像其他數字。
    """
    source = image.convert("RGB")
    output = Image.new(
        "RGB",
        source.size,
        "white",
    )

    source_pixels = source.load()
    output_pixels = output.load()

    for y in range(source.height):
        for x in range(source.width):
            red, green, blue = (
                source_pixels[x, y]
            )

            if not is_blue_pixel(
                red,
                green,
                blue,
            ):
                continue

            # 保留筆畫深淺，但把顏色拉成較清楚的深藍。
            brightness = int(
                (
                    red
                    + green
                    + blue
                )
                / 3
            )

            dark = max(
                0,
                min(
                    95,
                    brightness // 3,
                ),
            )

            output_pixels[x, y] = (
                dark,
                dark,
                min(180, dark + 100),
            )

    return output


def prepare_single_cell_images(
    cell: Image.Image,
) -> tuple[
    Image.Image,
    Image.Image,
]:
    """
    一個藍色格子產生兩張獨立高解析圖片：

    1. 原始藍筆緊密裁切圖
    2. 只保留藍筆的增強圖
    """
    bbox = get_blue_ink_bbox(cell)

    if bbox is None:
        original_crop = cell.convert(
            "RGB"
        )
        blue_crop = make_blue_only_image(
            cell
        )
    else:
        original_crop = cell.crop(
            bbox
        ).convert("RGB")

        blue_full = make_blue_only_image(
            cell
        )

        try:
            blue_crop = blue_full.crop(
                bbox
            ).convert("RGB")
        finally:
            blue_full.close()

    # 原圖只做溫和銳化，避免小數點被過度處理。
    original_crop = original_crop.filter(
        ImageFilter.SHARPEN
    )

    original_canvas = fit_image_to_canvas(
        original_crop,
        resample=Image.Resampling.LANCZOS,
    )

    blue_canvas = fit_image_to_canvas(
        blue_crop,
        resample=Image.Resampling.NEAREST,
    )

    original_crop.close()
    blue_crop.close()

    return (
        original_canvas,
        blue_canvas,
    )


def save_single_cell_debug_images(
    file_path: str,
    page_number: int,
    row_id: str,
    field: str,
    original_image: Image.Image,
    blue_image: Image.Image,
) -> None:
    if not SAVE_CHECK_DEBUG_IMAGES:
        return

    directory = os.path.join(
        os.path.dirname(
            os.path.abspath(
                file_path
            )
        ),
        "debug_check_cells",
    )

    os.makedirs(
        directory,
        exist_ok=True,
    )

    safe_row_id = re.sub(
        r"[^0-9a-zA-Z一-龥_-]",
        "_",
        row_id,
    )
    safe_field = re.sub(
        r"[^0-9a-zA-Z一-龥_-]",
        "_",
        field,
    )

    combined = Image.new(
        "RGB",
        (
            max(
                original_image.width,
                blue_image.width,
            ),
            original_image.height
            + blue_image.height
            + 20,
        ),
        "white",
    )

    combined.paste(
        original_image,
        (0, 0),
    )
    combined.paste(
        blue_image,
        (
            0,
            original_image.height + 20,
        ),
    )

    try:
        combined.save(
            os.path.join(
                directory,
                (
                    f"page_{page_number}_"
                    f"row_{safe_row_id}_"
                    f"field_{safe_field}.png"
                ),
            )
        )
    finally:
        combined.close()


def parse_single_cell_result(
    raw: str,
) -> tuple[str, str]:
    """
    寬鬆解析單一手寫格。

    原則：
    - 模型讀到什麼就保留什麼。
    - 不再因格式不完全、類型不符或內容可疑而改成 unclear。
    - JSON 格式壞掉時，直接保留模型原始文字。
    """
    try:
        data = parse_json_response(
            raw,
            "Generic Single Cell Agent",
        )
    except Exception:
        fallback = re.sub(
            r"```(?:json|text)?\s*|```",
            "",
            str(raw or ""),
            flags=re.IGNORECASE,
        ).strip()

        if fallback:
            return (
                "raw",
                fallback,
            )

        return (
            "raw",
            "辨識失敗",
        )

    if not isinstance(data, dict):
        fallback = str(data or "").strip()

        return (
            "raw",
            fallback or "辨識失敗",
        )

    kind = str(
        data.get("kind")
        or ""
    ).strip().lower()

    value = normalize_cell_value(
        data.get("value")
    )

    # 有些模型會改用其他 key，依序尋找可用內容。
    if value is None:
        for key in (
            "text",
            "result",
            "answer",
            "content",
        ):
            candidate = normalize_cell_value(
                data.get(key)
            )

            if candidate is not None:
                value = candidate
                break

    value_text = re.sub(
        r"\s+",
        "",
        str(value or ""),
    )

    check_values = {
        "✓",
        "✔",
        "☑",
        "check",
        "checked",
        "勾",
        "勾號",
        "通过",
        "通過",
        "v",
        "u",
    }

    cross_values = {
        "✗",
        "×",
        "x",
        "cross",
        "叉",
        "叉號",
    }

    if (
        kind == "check"
        or value_text.lower() in check_values
    ):
        return (
            "check",
            "✓",
        )

    if (
        kind == "cross"
        or value_text.lower() in cross_values
    ):
        return (
            "cross",
            "✗",
        )

    # 即使模型把 kind 寫錯、漏寫，仍直接保留它讀到的值。
    if value_text:
        return (
            kind or "raw",
            value_text,
        )

    # 模型仍回傳 unclear/unknown 或空值時，不再輸出 unclear；
    # 改為保留可供人工確認的誠實狀態。
    return (
        kind or "raw",
        "辨識失敗",
    )


CHECK_ONLY_GAUGES = {
    "eye",
    "eye1",
}

CHECK_ONLY_KEYWORDS = (
    "不可",
    "毛邊",
    "外觀",
    "夾持",
    "擺放",
    "核對樣品",
    "該對樣品",
    "目視",
)


def infer_expected_blue_kind(
    static_row: dict[str, Any],
) -> str:
    """
    根據固定印刷欄判斷右側藍筆內容應偏向：
    check / number / unknown。

    這個判斷只用來限制「數值列不能直接接受假勾號」，
    不會拿檢驗標準的實際數字去替模型猜答案。
    """
    gauge = normalize_key(
        static_row.get(
            "量具編號"
        )
    )
    standard = normalize_key(
        static_row.get(
            "檢驗標準"
        )
    )
    tolerance = normalize_key(
        static_row.get(
            "公差"
        )
    )

    if gauge in CHECK_ONLY_GAUGES:
        return "check"

    if any(
        keyword in standard
        for keyword in CHECK_ONLY_KEYWORDS
    ):
        return "check"

    if (
        re.search(r"\d", standard)
        or re.search(r"\d", tolerance)
    ):
        return "number"

    return "unknown"


def is_plausible_measurement(
    value: Any,
) -> bool:
    """
    判斷模型回傳內容是否像正常量測值。

    可接受：
    57.2、50.169、0.82、-0.15、30°

    拒絕：
    5/2、5/0、1234567890、571234、57A2
    """
    text = re.sub(
        r"\s+",
        "",
        str(value or ""),
    )

    if not text:
        return False

    if text in {
        "1234567890",
        "0123456789",
        "123456789",
    }:
        return False

    if not re.fullmatch(
        r"[+-]?"
        r"(?:"
        r"\d{1,4}(?:\.\d{1,5})?"
        r"|"
        r"\.\d{1,5}"
        r")"
        r"°?",
        text,
    ):
        return False

    number_part = (
        text
        .lstrip("+-")
        .rstrip("°")
    )

    # 沒有小數點時，四位以上通常是漏掉小數點或模型亂生。
    if (
        "." not in number_part
        and len(number_part) > 3
    ):
        return False

    return True

def read_one_blue_cell(
    file_path: str,
    page_number: int,
    row_id: str,
    field: str,
    cell: Image.Image,
    static_row: dict[str, Any],
) -> tuple[str, str]:
    """
    辨識單一藍色手寫格。

    本版採寬鬆、如實呈現模式：
    - 每格只辨識一次。
    - 不再要求兩次結果一致。
    - 不再用量測格式規則否決模型答案。
    - 模型讀到什麼就直接輸出什麼，交由人工確認。
    """
    (
        original_image,
        blue_image,
    ) = prepare_single_cell_images(
        cell
    )

    try:
        save_single_cell_debug_images(
            file_path,
            page_number,
            row_id,
            field,
            original_image,
            blue_image,
        )

        expected_kind = (
            infer_expected_blue_kind(
                static_row
            )
        )

        context = {
            "編號": row_id,
            "欄位": field,
            "預期內容種類": expected_kind,
        }

        prompt = f"""
你只辨識「一個」品質檢查表格子中的藍色手寫內容。

第一張圖片：藍筆原始內容的緊密裁切與等比例放大圖。
第二張圖片：同一內容只保留藍色筆跡的增強圖。

格子資訊：
{json.dumps(
    context,
    ensure_ascii=False,
    indent=2,
)}

請把圖片中真正看到的內容如實抄寫出來。

規則：
1. 單純 V、U 或勾形記號，輸出 kind="check"、value="✓"。
2. 其餘可見筆跡都輸出 kind="number"，value 必須照圖片抄寫。
3. 保留前導 0、小數點、正負號、括號與角度符號。
4. 不得拿格子資訊猜數值，但即使字跡模糊，也要輸出你認為最接近的讀法。
5. 不要輸出 unclear、unknown 或 null。
6. 只輸出合法 JSON，不要 Markdown，不要解釋。

輸出格式：
{{
  "kind": "number",
  "value": "這邊請輸入讀到的值"
}}

kind 只能是：check、cross、number。
"""

        raw = call_vlm(
            prompt,
            [
                image_to_base64(
                    original_image
                ),
                image_to_base64(
                    blue_image
                ),
            ],
            600,
        )

        chosen_kind, chosen_value = (
            parse_single_cell_result(
                raw
            )
        )

        combined_raw = json.dumps(
            {
                "expected_kind": expected_kind,
                "first": raw,
                "second": None,
                "chosen": {
                    "kind": chosen_kind,
                    "value": chosen_value,
                },
                "confidence": "best_effort",
                "review_required": True,
                "reason": (
                    "first_pass_as_read_no_unclear"
                ),
            },
            ensure_ascii=False,
        )

        return (
            chosen_value,
            combined_raw,
        )

    finally:
        original_image.close()
        blue_image.close()


def read_check_cells_for_row(
    file_path: str,
    page_number: int,
    table_image: Image.Image,
    check_boundaries: list[int],
    row_top: int,
    row_bottom: int,
    static_row: dict[str, Any],
    errors: list[str],
) -> tuple[dict[str, Any], str]:
    row_id = str(
        static_row.get("編號")
        or ""
    ).strip()

    result = {
        "編號": row_id,
        **{
            header: None
            for header in STANDARD_CHECK_HEADERS
        },
    }

    raw_results: dict[str, Any] = {}

    for index, field in enumerate(
        STANDARD_CHECK_HEADERS
    ):
        cell = crop_inner_cell(
            table_image,
            check_boundaries[index],
            row_top,
            check_boundaries[index + 1],
            row_bottom,
        )

        try:
            if not cell_has_blue_ink(
                cell
            ):
                continue

            try:
                value, raw = (
                    read_one_blue_cell(
                        file_path,
                        page_number,
                        row_id,
                        field,
                        cell,
                        static_row,
                    )
                )

                result[field] = value
                raw_results[field] = raw

                print(
                    "[Generic Cell] "
                    f"第 {page_number} 頁 "
                    f"編號 {row_id} "
                    f"欄位 {field}："
                    f"{value}"
                )

            except Exception as error:
                result[field] = "辨識失敗"

                errors.append(
                    f"第 {page_number} 頁"
                    f"編號 {row_id} "
                    f"欄位 {field} "
                    "單格辨識失敗："
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        finally:
            cell.close()

    return (
        result,
        json.dumps(
            raw_results,
            ensure_ascii=False,
        )
        if raw_results
        else "",
    )


def merge_static_with_fixed_checks(
    static_rows: list[dict[str, Any]],
    check_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    左側固定欄與右側手寫欄依「實體列順序」合併。

    不再用編號建立 dictionary。因為多筆編號讀成 unclear，
    或原稿真的出現重複編號時，dictionary 會覆蓋前一列，
    導致手寫結果被接到錯誤資料列。
    """
    output: list[dict[str, Any]] = []

    for index, static_row in enumerate(
        static_rows
    ):
        merged = {
            header: None
            for header in STANDARD_HEADERS
        }

        source = {
            normalize_key(key): value
            for key, value in (
                static_row or {}
            ).items()
        }

        for header in STANDARD_STATIC_HEADERS:
            merged[header] = (
                normalize_cell_value(
                    source.get(
                        normalize_key(header)
                    )
                )
            )

        check_row = (
            check_rows[index]
            if index < len(check_rows)
            else {}
        )

        for header in STANDARD_CHECK_HEADERS:
            merged[header] = (
                normalize_cell_value(
                    check_row.get(header)
                )
            )

        output.append(merged)

    return output


# ============================================================
# 格線先行與逐列固定欄辨識
# ============================================================

SAVE_STATIC_ROW_DEBUG_IMAGES = True
STATIC_ROW_CANVAS_SIZE = (
    2048,
    320,
)


def reset_generic_document_cache(
    file_path: str | None = None,
) -> None:
    """
    批次處理不同檔案時可呼叫，避免舊方向或 X 邊界污染新檔案。
    """
    if file_path is None:
        _DOCUMENT_CACHE.clear()
        return

    _DOCUMENT_CACHE.pop(
        os.path.abspath(file_path),
        None,
    )


def canonicalize_static_row(
    raw_row: dict[str, Any] | None,
) -> dict[str, Any]:
    source = {
        normalize_key(key): value
        for key, value in (
            raw_row or {}
        ).items()
    }

    return {
        header: normalize_cell_value(
            source.get(
                normalize_key(header)
            )
        )
        for header in STANDARD_STATIC_HEADERS
    }


def is_valid_static_row_id(
    value: Any,
) -> bool:
    """
    此類檢查表的編號使用 1～3 位阿拉伯數字。

    將上限限制為 3 位，可排除模型把多個相鄰數字黏成
    1234 之類的幻覺答案。
    """
    text = normalize_row_id(
        value
    )

    return bool(
        re.fullmatch(
            r"\d{1,3}",
            text,
        )
    )


def static_row_has_meaningful_content(
    row: dict[str, Any],
) -> bool:
    """
    判斷編號以外是否至少有一個真正的固定欄內容。

    空白預印列通常只有編號，其他欄全部空白；這種列不輸出。
    """
    for header in STANDARD_STATIC_HEADERS[1:]:
        value = normalize_cell_value(
            row.get(header)
        )

        if value is None:
            continue

        if normalize_key(value) in {
            "unclear",
            "unknown",
            "看不清楚",
            "不清楚",
        }:
            continue

        return True

    return False


def select_all_row_boundaries(
    horizontal_lines: list[int],
    image_height: int,
) -> list[int]:
    """
    不再先問 VLM 有幾列。

    直接從表格橫線找出：
    Header 上緣、Header 下緣，以及所有固定資料列下緣。

    候選以「可形成最長且等高的資料列序列」優先。
    """
    candidates: list[
        tuple[
            int,
            float,
            float,
            list[int],
        ]
    ] = []

    for start in range(
        max(0, len(horizontal_lines) - 2)
    ):
        for end in range(
            start + 3,
            len(horizontal_lines),
        ):
            sequence = horizontal_lines[
                start:end + 1
            ]

            # 第一段是 Header 高度；第二段起才是資料列高度。
            data_gaps = [
                sequence[index + 1]
                - sequence[index]
                for index in range(
                    1,
                    len(sequence) - 1,
                )
            ]

            if len(data_gaps) < 2:
                continue

            sorted_gaps = sorted(
                data_gaps
            )
            median_gap = float(
                sorted_gaps[
                    len(sorted_gaps) // 2
                ]
            )

            if not (
                12
                <= median_gap
                <= image_height * 0.18
            ):
                continue

            # 資料列應接近等高。設定較嚴格，避免把 Header 或頁首列混進來。
            if any(
                gap < median_gap * 0.84
                or gap > median_gap * 1.16
                for gap in data_gaps
            ):
                continue

            # 主表通常延伸到裁切圖片底部附近。
            if sequence[-1] < image_height * 0.84:
                continue

            header_gap = (
                sequence[1]
                - sequence[0]
            )

            if not (
                8
                <= header_gap
                <= median_gap * 2.20
            ):
                continue

            deviation = sum(
                abs(gap - median_gap)
                for gap in data_gaps
            ) / (
                len(data_gaps)
                * median_gap
            )

            bottom_margin = (
                image_height - sequence[-1]
            ) / max(image_height, 1)

            # Python 由小到大排序；負列數代表列數越多越優先。
            candidates.append(
                (
                    -len(data_gaps),
                    deviation,
                    bottom_margin,
                    list(sequence),
                )
            )

    if not candidates:
        # 放寬一次，處理掃描歪斜造成的列高波動。
        for start in range(
            max(0, len(horizontal_lines) - 2)
        ):
            for end in range(
                start + 3,
                len(horizontal_lines),
            ):
                sequence = horizontal_lines[
                    start:end + 1
                ]
                data_gaps = [
                    sequence[index + 1]
                    - sequence[index]
                    for index in range(
                        1,
                        len(sequence) - 1,
                    )
                ]

                if len(data_gaps) < 2:
                    continue

                sorted_gaps = sorted(
                    data_gaps
                )
                median_gap = float(
                    sorted_gaps[
                        len(sorted_gaps) // 2
                    ]
                )

                if not (
                    12
                    <= median_gap
                    <= image_height * 0.18
                ):
                    continue

                if any(
                    gap < median_gap * 0.75
                    or gap > median_gap * 1.28
                    for gap in data_gaps
                ):
                    continue

                if sequence[-1] < image_height * 0.80:
                    continue

                deviation = sum(
                    abs(gap - median_gap)
                    for gap in data_gaps
                ) / (
                    len(data_gaps)
                    * median_gap
                )
                bottom_margin = (
                    image_height - sequence[-1]
                ) / max(image_height, 1)

                candidates.append(
                    (
                        -len(data_gaps),
                        deviation,
                        bottom_margin,
                        list(sequence),
                    )
                )

    if not candidates:
        return []

    candidates.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
        )
    )

    return candidates[0][3]


# 固定欄位的實際幾何結構：
#
# 編號 | 量具編號 | 代號 | 刀號 | 檢驗標準 | 公差(符號|數值)
#      | 檢查人員(內部可能再分線) | 首件 | 1 ... 11
#
# 因此從主表左緣到「首件」左緣，一共會形成 9 個區段、
# 10 條主要邊界。公差與檢查人員各自可能含一條內部分隔線。
STATIC_LAYOUT_TARGET_GAPS = [
    0.060,
    0.245,
    0.062,
    0.063,
    0.218,
    0.049,
    0.130,
    0.049,
    0.123,
]


def select_static_column_layout(
    vertical_lines: list[int],
    check_start: int,
) -> list[int]:
    """
    找出左側固定欄與「檢查人員」欄的 10 條幾何邊界。

    回傳順序：
    0 主表左緣
    1 編號右緣
    2 量具編號右緣
    3 代號右緣
    4 刀號右緣
    5 檢驗標準右緣
    6 公差內部分隔線
    7 公差右緣
    8 檢查人員內部分隔線
    9 首件左緣

    修正重點：
    1. 只要能用影像中真正找到的左外框形成合法 layout，
       就不再讓人工 x=0 候選與它競爭。
    2. 真正缺少左外框時，才依第一欄目標比例反推左邊界。
    """
    pre_lines = [
        int(value)
        for value in vertical_lines
        if 0 <= value < check_start - 4
    ]

    gap_ranges = [
        (0.020, 0.130),
        (0.160, 0.360),
        (0.025, 0.110),
        (0.025, 0.110),
        (0.140, 0.300),
        (0.020, 0.090),
        (0.070, 0.200),
        (0.020, 0.090),
        (0.070, 0.200),
    ]

    weights = [
        0.5,
        0.7,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
    ]

    def score_candidate(
        sequence: list[int],
    ) -> tuple[float, list[int]] | None:
        if len(sequence) != 10:
            return None

        if any(
            sequence[index + 1]
            <= sequence[index]
            for index in range(9)
        ):
            return None

        total_width = (
            sequence[-1]
            - sequence[0]
        )

        if total_width <= 0:
            return None

        gap_ratios = [
            (
                sequence[index + 1]
                - sequence[index]
            )
            / total_width
            for index in range(9)
        ]

        if any(
            not lower <= ratio <= upper
            for ratio, (
                lower,
                upper,
            ) in zip(
                gap_ratios,
                gap_ranges,
            )
        ):
            return None

        score = sum(
            weight
            * abs(ratio - target)
            / max(target, 0.02)
            for ratio, target, weight
            in zip(
                gap_ratios,
                STATIC_LAYOUT_TARGET_GAPS,
                weights,
            )
        )

        return (
            score,
            list(sequence),
        )

    # ==========================================
    # 第一優先：真正辨識到的左外框
    # ==========================================
    real_candidates: list[
        tuple[float, list[int]]
    ] = []

    for start in range(
        max(
            0,
            len(pre_lines) - 9 + 1,
        )
    ):
        candidate = score_candidate(
            [
                *pre_lines[
                    start:start + 9
                ],
                check_start,
            ]
        )

        if candidate is not None:
            real_candidates.append(
                candidate
            )

    if real_candidates:
        real_candidates.sort(
            key=lambda item: item[0]
        )
        return real_candidates[0][1]

    # ==========================================
    # 第二優先：只找到 8 條前置線時，判斷究竟漏了哪一條
    # ==========================================
    #
    # 舊版只假設「漏掉主表左外框」，因此可能把真正左外框
    # 誤當成「編號右緣」。例如：
    # [68, 564, 669, ...]
    # 會被錯補成：
    # [0, 68, 564, ...]
    # 導致編號格實際裁到表格外側 0:68，模型只看到一條直線，
    # 最後大量誤讀成 1。
    #
    # 現在同時評估：
    # A. 漏掉左外框
    # B. 左外框存在，但漏掉任一內部分隔線
    # 最後由整體欄寬比例選出分數最低者。
    inferred_candidates: list[
        tuple[float, list[int]]
    ] = []

    for start in range(
        max(
            0,
            len(pre_lines) - 8 + 1,
        )
    ):
        visible_lines = pre_lines[
            start:start + 8
        ]

        if len(visible_lines) != 8:
            continue

        # ------------------------------------------
        # A. 可能漏掉主表左外框
        # ------------------------------------------
        first_separator = visible_lines[0]
        target_ratio = (
            STATIC_LAYOUT_TARGET_GAPS[0]
        )

        inferred_left = int(
            round(
                (
                    first_separator
                    - target_ratio
                    * check_start
                )
                / max(
                    1.0 - target_ratio,
                    0.01,
                )
            )
        )

        inferred_left = max(
            0,
            min(
                first_separator - 4,
                inferred_left,
            ),
        )

        candidate = score_candidate(
            [
                inferred_left,
                *visible_lines,
                check_start,
            ]
        )

        if candidate is not None:
            inferred_candidates.append(
                candidate
            )

        # ------------------------------------------
        # B. 第一條可見線可能就是真正左外框，
        #    而中間少偵測到一條分隔線
        # ------------------------------------------
        left_border = visible_lines[0]
        total_width = (
            check_start - left_border
        )

        if total_width <= 0:
            continue

        cumulative_targets: list[float] = []
        cumulative = 0.0

        for gap_ratio in STATIC_LAYOUT_TARGET_GAPS:
            cumulative += gap_ratio
            cumulative_targets.append(
                cumulative
            )

        # 邊界 index 1～8 都可能是漏掉的內部分隔線。
        # index 9 是已知的 check_start，不需要推估。
        for missing_index in range(
            1,
            9,
        ):
            inferred_boundary = int(
                round(
                    left_border
                    + cumulative_targets[
                        missing_index - 1
                    ]
                    * total_width
                )
            )

            sequence = list(
                visible_lines
            )

            if not (
                sequence[
                    missing_index - 1
                ]
                + 4
                < inferred_boundary
                < sequence[
                    missing_index
                ]
                - 4
            ):
                continue

            sequence.insert(
                missing_index,
                inferred_boundary,
            )
            sequence.append(
                check_start
            )

            candidate = score_candidate(
                sequence
            )

            if candidate is not None:
                inferred_candidates.append(
                    candidate
                )

    if not inferred_candidates:
        return []

    inferred_candidates.sort(
        key=lambda item: item[0]
    )

    return inferred_candidates[0][1]


def get_static_column_layout(
    file_path: str,
    table_image: Image.Image,
    check_start: int,
) -> list[int]:
    """
    同一份 PDF 的固定欄 X 座標可以沿用。
    """
    cache_key = os.path.abspath(
        file_path
    )
    document_cache = (
        _DOCUMENT_CACHE.setdefault(
            cache_key,
            {},
        )
    )

    cached = document_cache.get(
        "generic_static_x_ratios",
        [],
    )

    if (
        isinstance(cached, list)
        and len(cached) == 10
    ):
        layout = scale_cached_boundaries(
            cached,
            table_image.width,
        )

        # 首件左緣必須與已找到的檢查欄起點一致。
        layout[-1] = check_start
    else:
        vertical_lines = (
            find_vertical_grid_lines(
                table_image
            )
        )

        layout = (
            select_static_column_layout(
                vertical_lines,
                check_start,
            )
        )

    if len(layout) != 10:
        document_cache.pop(
            "generic_static_x_ratios",
            None,
        )
        raise ValueError(
            "無法定位編號、量具編號、代號、刀號、"
            "檢驗標準與公差的固定垂直邊界"
        )

    if layout[-1] != check_start:
        layout[-1] = check_start

    document_cache[
        "generic_static_x_ratios"
    ] = [
        value / max(
            table_image.width,
            1,
        )
        for value in layout
    ]

    print(
        "[Generic Grid] "
        "固定印刷欄範圍："
        f"{layout[0]}:{layout[7]}，"
        "檢查人員欄："
        f"{layout[7]}:{layout[9]}"
    )
    print(
        "[Generic Grid] "
        f"固定欄完整邊界：{layout}"
    )

    return layout


def get_grid_first_boundaries(
    file_path: str,
    table_image: Image.Image,
) -> tuple[list[int], list[int]]:
    """
    X 邊界可在同一份 PDF 中沿用；Y 邊界每頁重新偵測。
    不再讓 VLM 的 row_count 決定格線。
    """
    cache_key = os.path.abspath(
        file_path
    )
    document_cache = (
        _DOCUMENT_CACHE.setdefault(
            cache_key,
            {},
        )
    )

    cached_x = document_cache.get(
        "generic_check_x_ratios",
        [],
    )

    if (
        isinstance(cached_x, list)
        and len(cached_x) == 13
    ):
        check_boundaries = (
            scale_cached_boundaries(
                cached_x,
                table_image.width,
            )
        )
    else:
        check_boundaries = (
            select_check_column_boundaries(
                find_vertical_grid_lines(
                    table_image
                ),
                table_image.width,
            )
        )

    if len(check_boundaries) != 13:
        document_cache.pop(
            "generic_check_x_ratios",
            None,
        )
        raise ValueError(
            "無法定位首件、1～11 的 13 條垂直邊界"
        )

    start_ratio = (
        check_boundaries[0]
        / max(table_image.width, 1)
    )
    end_ratio = (
        check_boundaries[-1]
        / max(table_image.width, 1)
    )

    if (
        start_ratio < 0.35
        or end_ratio < 0.82
    ):
        document_cache.pop(
            "generic_check_x_ratios",
            None,
        )
        raise ValueError(
            "檢查欄位不在主表右側，可能是文件方向錯誤"
        )

    horizontal_lines = (
        find_horizontal_grid_lines(
            table_image,
            dark_threshold=170,
            min_dark_ratio=0.20,
            merge_gap=8,
        )
    )
    row_boundaries = (
        select_all_row_boundaries(
            horizontal_lines,
            table_image.height,
        )
    )

    if len(row_boundaries) < 4:
        raise ValueError(
            "無法從格線找出固定資料列"
        )

    document_cache[
        "generic_check_x_ratios"
    ] = [
        value / table_image.width
        for value in check_boundaries
    ]

    print(
        "[Generic Grid] "
        f"固定檢查欄範圍："
        f"{check_boundaries[0]}:"
        f"{check_boundaries[-1]}"
    )
    print(
        "[Generic Grid] "
        f"格線辨識到 {len(row_boundaries) - 2} 個資料列，"
        f"範圍：{row_boundaries[1]}:"
        f"{row_boundaries[-1]}"
    )

    return check_boundaries, row_boundaries


def clean_pipe_value(
    value: Any,
) -> Any:
    text = str(value or "").strip()

    text = re.sub(
        r"^(編號|量具編號|代號|刀號|檢驗標準|公差)\s*[：:]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    if normalize_key(text) in {
        "",
        "null",
        "none",
        "空白",
        "無",
    }:
        return None

    if normalize_key(text) in {
        "unclear",
        "unknown",
        "看不清楚",
        "不清楚",
    }:
        return "unclear"

    return text


def static_cell_dark_ratio(
    cell: Image.Image,
) -> float:
    """
    格線已經由 crop_inner_cell 移除，只估計格內印刷內容密度。
    """
    gray = cell.convert("L")

    try:
        values = list(
            gray.getdata()
        )

        if not values:
            return 0.0

        return sum(
            value < 145
            for value in values
        ) / len(values)

    finally:
        gray.close()


def join_static_subcells(
    left_cell: Image.Image,
    right_cell: Image.Image,
    separator: int = 14,
) -> Image.Image:
    """
    公差常拆成「符號」與「數值」兩個實體格。
    先各自去除格線，再用白色間距拼回同一張圖片。
    """
    output = Image.new(
        "RGB",
        (
            left_cell.width
            + separator
            + right_cell.width,
            max(
                left_cell.height,
                right_cell.height,
            ),
        ),
        "white",
    )

    output.paste(
        left_cell,
        (0, 0),
    )
    output.paste(
        right_cell,
        (
            left_cell.width
            + separator,
            0,
        ),
    )

    return output


def crop_static_inner_cell(
    image: Image.Image,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> Image.Image:
    """
    固定印刷欄專用裁切。

    印刷文字通常置中，不像藍筆會貼近格線，因此可使用較大的
    內縮量，把殘留格線與掃描陰影移除。這能避免空白刀號被
    模型幻覺成 T，也能避免空白公差符號被讀成 + 或 -。
    """
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)

    margin_x = max(2, int(width * 0.07))
    margin_y = max(2, int(height * 0.10))

    left = min(x1 - 1, x0 + margin_x)
    right = max(left + 1, x1 - margin_x)
    top = min(y1 - 1, y0 + margin_y)
    bottom = max(top + 1, y1 - margin_y)

    return image.crop(
        (left, top, right, bottom)
    ).convert("RGB")


def crop_static_cells_for_row(
    table_image: Image.Image,
    static_layout: list[int],
    row_top: int,
    row_bottom: int,
) -> list[Image.Image]:
    """
    將同一列固定切成六張完全獨立的圖片。

    圖片順序永遠是：
    編號、量具編號、代號、刀號、檢驗標準、公差。
    """
    if len(static_layout) != 10:
        raise ValueError(
            "固定欄位 layout 必須有 10 條邊界"
        )

    cells = [
        crop_static_inner_cell(
            table_image,
            static_layout[index],
            row_top,
            static_layout[index + 1],
            row_bottom,
        )
        for index in range(5)
    ]

    # 公差由「符號格」與「數值格」組成。先各自去除格線，
    # 再拼成同一欄；空白符號格不會再殘留邊框造成 + / - 幻覺。
    tolerance_sign = crop_static_inner_cell(
        table_image,
        static_layout[5],
        row_top,
        static_layout[6],
        row_bottom,
    )
    tolerance_value = crop_static_inner_cell(
        table_image,
        static_layout[6],
        row_top,
        static_layout[7],
        row_bottom,
    )

    try:
        tolerance = join_static_subcells(
            tolerance_sign,
            tolerance_value,
        )
    finally:
        tolerance_sign.close()
        tolerance_value.close()

    cells.append(tolerance)
    return cells


def prepare_static_cell_view(
    cell: Image.Image,
    header: str,
) -> Image.Image:
    """
    每個欄位獨立放大。模型不再觀看整列，因此空白欄不會使
    後方內容向左位移。
    """
    gray = ImageOps.autocontrast(
        cell.convert("L"),
        cutoff=1,
    )
    enhanced = gray.convert(
        "RGB"
    ).filter(
        ImageFilter.SHARPEN
    )
    gray.close()

    canvas_sizes = {
        "編號": (560, 420),
        "量具編號": (900, 420),
        "代號": (560, 420),
        "刀號": (700, 420),
        "檢驗標準": (1450, 420),
        "公差": (1000, 420),
    }

    try:
        return fit_image_to_canvas(
            enhanced,
            canvas_size=canvas_sizes[
                header
            ],
            margin=22,
            resample=Image.Resampling.LANCZOS,
        )
    finally:
        enhanced.close()


def save_static_cell_debug_images(
    file_path: str,
    page_number: int,
    row_index: int,
    headers: list[str],
    images: list[Image.Image],
) -> None:
    if not SAVE_STATIC_ROW_DEBUG_IMAGES:
        return

    directory = os.path.join(
        os.path.dirname(
            os.path.abspath(
                file_path
            )
        ),
        "debug_static_cells",
    )
    os.makedirs(
        directory,
        exist_ok=True,
    )

    for header, image in zip(
        headers,
        images,
    ):
        safe_header = re.sub(
            r"[^0-9a-zA-Z一-龥_-]",
            "_",
            header,
        )

        image.save(
            os.path.join(
                directory,
                (
                    f"page_{page_number}_"
                    f"row_{row_index:02d}_"
                    f"field_{safe_header}.png"
                ),
            )
        )


def static_field_plausibility_score(
    header: str,
    value: Any,
) -> int:
    value = normalize_cell_value(
        value
    )

    if value is None:
        # 空白本身是合法答案，不應因空白欄而把後面內容往前補。
        return 1

    text = str(value).strip()
    normalized = normalize_key(text)

    if normalized in {
        "unclear",
        "unknown",
    }:
        return 0

    if header == "編號":
        return (
            14
            if is_valid_static_row_id(text)
            else -20
        )

    if header == "量具編號":
        # 尺寸符號或完整尺寸通常不可能是量具代碼。
        if re.search(
            r"[φø∅±°]",
            text,
        ):
            return -6

        if re.fullmatch(
            r"[-+]?\d+(?:\.\d+)?",
            text,
        ):
            return -5

        return 5 if len(text) <= 18 else 1

    if header == "代號":
        if re.search(
            r"[φø∅±°]",
            text,
        ):
            return -6

        return (
            6
            if len(text) <= 6
            else -2
        )

    if header == "刀號":
        return (
            4
            if len(text) <= 16
            else 0
        )

    if header == "檢驗標準":
        return 7

    if header == "公差":
        return 5

    return 0


def choose_static_field_value(
    header: str,
    first_value: Any,
    second_value: Any,
) -> Any:
    """
    第二輪不是整列覆蓋第一輪，而是逐欄比較。
    """
    first_score = (
        static_field_plausibility_score(
            header,
            first_value,
        )
    )
    second_score = (
        static_field_plausibility_score(
            header,
            second_value,
        )
    )

    if second_score > first_score:
        return normalize_cell_value(
            second_value
        )

    return normalize_cell_value(
        first_value
    )


def static_cell_is_visually_blank(
    cell: Image.Image,
) -> bool:
    """
    在送 VLM 前先用影像密度排除真正空白格。

    固定欄已經用較大 margin 移除格線；剩餘深色像素低於 0.35%
    時可安全視為空白。
    """
    return static_cell_dark_ratio(cell) < 0.0035


def normalize_tolerance_text(
    value: Any,
) -> Any:
    value = normalize_cell_value(value)

    if value is None:
        return None

    text = str(value).strip()
    text = (
        text.replace("＋", "+")
        .replace("－", "-")
        .replace("−", "-")
        .replace("–", "-")
        .replace("﹢", "+")
        .replace("﹣", "-")
    )
    text = re.sub(r"\s+", "", text)

    # 常見的同義寫法統一為 ±；單獨的 +0.2 / -0.2 保留。
    text = re.sub(
        r"^(?:\+/-|\+-|-\+|\+∕-|\+／-)",
        "±",
        text,
    )
    text = text.replace("±±", "±")

    return text or None


def normalize_static_field_value(
    header: str,
    value: Any,
) -> Any:
    value = clean_pipe_value(value)

    if value is None:
        return None

    if normalize_key(value) in {
        "unclear",
        "unknown",
        "看不清楚",
        "不清楚",
    }:
        return "unclear"

    text = str(value).strip()

    if header == "編號":
        # 只接受「整個回答本身就是一個 1～3 位數編號」。
        #
        # 舊版使用 re.sub(r"\\D", "", text) 抽取所有數字，
        # 模型若回答「圖片1」「第1格」也會被錯誤轉成編號 1。
        cleaned = re.sub(
            r"^(?:編號|row|id)\s*[：:#-]?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        match = re.fullmatch(
            r"(\d{1,3})\s*[\.、,，)]?",
            cleaned,
        )

        if match:
            return match.group(1)

        return "unclear"

    if header == "公差":
        return normalize_tolerance_text(text)

    return text


def build_static_field_prompt(
    header: str,
    retry: bool = False,
) -> str:
    retry_text = (
        "這是第二次獨立複判。請重新看圖片，不得沿用第一次答案。"
        if retry
        else ""
    )

    field_rules = {
        "編號": (
            "只抄寫資料列編號。通常是 1 到 3 位阿拉伯數字；"
            "不得抄寫其他欄位。"
        ),
        "量具編號": (
            "只抄寫量具代碼，例如 H、M、PM、PG、DN、CMM、SJ、"
            "eye、eye1、H-F。尺寸值、φ、± 不屬於本欄。"
        ),
        "代號": (
            "只抄寫短代號，例如 C、D、F、H、DC。不得抄尺寸值。"
        ),
        "刀號": (
            "只抄寫刀號，例如 T2、T6 或數字。圖片空白必須輸出 null。"
        ),
        "檢驗標準": (
            "只抄寫檢驗標準，完整保留中文字、φ/ø、數字、小數點、"
            "角度符號與 x/×。"
        ),
        "公差": (
            "只抄寫公差，完整保留 ±、+、-、數字、小數點、Ra、"
            "以上、以下、以內。不得自行計算。"
        ),
    }

    return f"""
你正在辨識品質檢查表中的單一固定欄位。

欄位名稱：{header}
{retry_text}

規則：
1. 圖片只包含「{header}」這一格，不得猜測其他欄位。
2. {field_rules[header]}
3. 圖片真正空白時輸出 null。
4. 看得到內容但無法確定時才輸出 unclear。
5. 只輸出合法 JSON，不要 Markdown，不要解釋。

固定格式：
{{"value": null}}
"""


def parse_static_field_result(
    raw: str,
    header: str,
) -> Any:
    try:
        data = parse_json_response(
            raw,
            f"Generic Static {header} Agent",
        )
    except Exception:
        data = None

    if isinstance(data, dict):
        if "value" in data:
            return normalize_static_field_value(
                header,
                data.get("value"),
            )

        if header in data:
            return normalize_static_field_value(
                header,
                data.get(header),
            )

    text = str(raw or "").strip()
    text = re.sub(
        r"```(?:json|text)?\s*|```",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    return normalize_static_field_value(
        header,
        text,
    )


def static_field_acceptance_score(
    header: str,
) -> int:
    return {
        "編號": 10,
        "量具編號": 3,
        "代號": 3,
        "刀號": 1,
        "檢驗標準": 3,
        "公差": 3,
    }[header]


def make_static_retry_view(
    cell: Image.Image,
    header: str,
) -> Image.Image:
    """第二輪使用另一種對比處理，避免兩輪完全相同。"""
    gray = ImageOps.autocontrast(
        cell.convert("L"),
        cutoff=0,
    )
    enhanced = gray.filter(
        ImageFilter.SHARPEN
    ).filter(
        ImageFilter.SHARPEN
    ).convert("RGB")
    gray.close()

    canvas_sizes = {
        "編號": (560, 420),
        "量具編號": (900, 420),
        "代號": (560, 420),
        "刀號": (700, 420),
        "檢驗標準": (1450, 420),
        "公差": (1000, 420),
    }

    try:
        return fit_image_to_canvas(
            enhanced,
            canvas_size=canvas_sizes[header],
            margin=22,
            resample=Image.Resampling.NEAREST,
        )
    finally:
        enhanced.close()


def read_one_static_field(
    header: str,
    cell: Image.Image,
) -> tuple[Any, dict[str, Any]]:
    """
    一張圖片只辨識一個欄位。

    編號欄是細小的固定印刷數字，不使用一般固定欄的
    影像空白門檻提前擋掉；其他欄位仍先排除真正空白格。
    """
    if (
        header != "編號"
        and static_cell_is_visually_blank(
            cell
        )
    ):
        return None, {
            "header": header,
            "blank_by_image": True,
            "dark_ratio": static_cell_dark_ratio(cell),
        }

    first_view = prepare_static_cell_view(
        cell,
        header,
    )

    try:
        first_raw = call_vlm(
            build_static_field_prompt(
                header,
                retry=False,
            ),
            [image_to_base64(first_view)],
            600,
        )
        first_value = parse_static_field_result(
            first_raw,
            header,
        )
        first_score = static_field_plausibility_score(
            header,
            first_value,
        )

        # 編號不採用第一次單次結果直接通過。
        # 它是合併資料的關鍵欄位，必須再做一次獨立複判，
        # 避免單一格線被誤認成 1 後直接寫入。
        if (
            header != "編號"
            and first_score
            >= static_field_acceptance_score(
                header
            )
        ):
            return first_value, {
                "header": header,
                "first": first_raw,
                "second": None,
                "chosen": first_value,
            }

    finally:
        first_view.close()

    retry_view = make_static_retry_view(
        cell,
        header,
    )

    try:
        second_raw = call_vlm(
            build_static_field_prompt(
                header,
                retry=True,
            ),
            [image_to_base64(retry_view)],
            600,
            model_name=RECHECK_MODEL_NAME,
        )
        second_value = parse_static_field_result(
            second_raw,
            header,
        )

    finally:
        retry_view.close()

    if header == "編號":
        first_id = normalize_row_id(
            first_value
        )
        second_id = normalize_row_id(
            second_value
        )
        first_valid = is_valid_static_row_id(
            first_value
        )
        second_valid = is_valid_static_row_id(
            second_value
        )

        if (
            first_valid
            and second_valid
            and first_id == second_id
        ):
            chosen = first_id
        elif first_valid and not second_valid:
            chosen = first_id
        elif second_valid and not first_valid:
            chosen = second_id
        else:
            # 兩次都是合法數字但彼此不同時，不隨便挑其中一個。
            chosen = "unclear"
    else:
        chosen = choose_static_field_value(
            header,
            first_value,
            second_value,
        )

    # 兩輪都明顯不合理時，不把幻覺字串寫進正式欄位。
    if static_field_plausibility_score(
        header,
        chosen,
    ) < 0:
        chosen = "unclear"

    return chosen, {
        "header": header,
        "first": first_raw,
        "second": second_raw,
        "chosen": chosen,
    }


def static_row_has_visual_content(
    cells: list[Image.Image],
) -> bool:
    """
    用影像本身判斷資料列是否真的有固定欄內容。

    只看編號以外的固定欄，避免「只有預印編號」的空白列
    被輸出。判斷採兩級門檻：一格內容很明顯，或至少兩格
    有中等內容時才保留；少量掃描雜訊不算有效資料列。
    """
    if len(cells) < 2:
        return False

    ratios = [
        static_cell_dark_ratio(cell)
        for cell in cells[1:]
    ]

    strong_count = sum(
        ratio >= 0.0035
        for ratio in ratios
    )

    medium_count = sum(
        ratio >= 0.0018
        for ratio in ratios
    )

    return (
        strong_count >= 1
        or medium_count >= 2
    )


def row_has_blue_check_content(
    table_image: Image.Image,
    check_boundaries: list[int],
    row_top: int,
    row_bottom: int,
) -> bool:
    """
    即使左側固定欄很淡，只要右側存在藍筆，也必須保留該實體列。
    """
    for index in range(
        len(STANDARD_CHECK_HEADERS)
    ):
        cell = crop_inner_cell(
            table_image,
            check_boundaries[index],
            row_top,
            check_boundaries[index + 1],
            row_bottom,
        )

        try:
            if cell_has_blue_ink(cell):
                return True
        finally:
            cell.close()

    return False


def make_unclear_static_row_from_cells(
    cells: list[Image.Image],
) -> dict[str, Any]:
    """
    固定欄 Agent 整列失敗時建立保留用資料列。

    有可見內容的格子標記 unclear，真正空白格維持 null。
    """
    row: dict[str, Any] = {}

    for header, cell in zip(
        STANDARD_STATIC_HEADERS,
        cells,
    ):
        if header == "編號":
            row[header] = "unclear"
        elif static_cell_dark_ratio(cell) >= 0.0012:
            row[header] = "unclear"
        else:
            row[header] = None

    return canonicalize_static_row(row)


def read_one_static_row(
    file_path: str,
    page_number: int,
    row_index: int,
    cells: list[Image.Image],
    force_preserve: bool = False,
) -> tuple[
    dict[str, Any] | None,
    str,
]:
    if len(cells) != 6:
        raise ValueError(
            "固定欄位必須切成六張圖片"
        )

    has_static_visual_content = (
        static_row_has_visual_content(
            cells
        )
    )

    # 真正空白的預印列才跳過。只要左側有可見內容，或右側有藍筆，
    # 後續就算 VLM 全部失敗，也必須保留這個實體列。
    if (
        not has_static_visual_content
        and not force_preserve
    ):
        return None, json.dumps(
            {
                "row": row_index,
                "chosen": None,
                "reason": (
                    "編號以外固定欄無可見內容，"
                    "且右側沒有藍筆，判定為空白預印列"
                ),
            },
            ensure_ascii=False,
        )

    # Debug 仍保存六個欄位的放大圖。
    debug_views = [
        prepare_static_cell_view(
            cell,
            header,
        )
        for header, cell in zip(
            STANDARD_STATIC_HEADERS,
            cells,
        )
    ]

    try:
        save_static_cell_debug_images(
            file_path,
            page_number,
            row_index,
            STANDARD_STATIC_HEADERS,
            debug_views,
        )
    finally:
        for image in debug_views:
            image.close()

    chosen: dict[str, Any] = {}
    field_logs: list[dict[str, Any]] = []

    # 每一格獨立呼叫；一張圖片只允許回答一個欄位。
    for header, cell in zip(
        STANDARD_STATIC_HEADERS,
        cells,
    ):
        try:
            value, field_log = read_one_static_field(
                header,
                cell,
            )
        except Exception as error:
            value = "unclear"
            field_log = {
                "header": header,
                "error": (
                    f"{type(error).__name__}: {error}"
                ),
                "chosen": "unclear",
            }

        chosen[header] = value
        field_logs.append(field_log)

    chosen = canonicalize_static_row(chosen)

    # 是否保留由影像決定，不再由 VLM 是否成功讀到文字決定。
    # 因此真實資料列即使五個固定欄都回傳 unclear，也不會消失。

    # 有內容但編號辨識失敗時仍保留實體列，不再默默消失。
    if not is_valid_static_row_id(
        chosen.get("編號")
    ):
        chosen["編號"] = "unclear"

    return chosen, json.dumps(
        {
            "row": row_index,
            "fields": field_logs,
            "chosen": chosen,
        },
        ensure_ascii=False,
    )


def read_static_rows_grid_first(
    file_path: str,
    page_number: int,
    table_image: Image.Image,
    static_layout: list[int],
    check_boundaries: list[int],
    row_boundaries: list[int],
    errors: list[str],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[int, int]],
    list[str],
]:
    """
    先用格線固定切出六個印刷欄，再把六張獨立格子圖交給 VLM。
    """
    rows: list[
        dict[str, Any]
    ] = []
    ranges: list[
        tuple[int, int]
    ] = []
    raw_parts: list[str] = []
    seen_ids: set[str] = set()

    row_count = len(
        row_boundaries
    ) - 2

    for row_offset in range(
        row_count
    ):
        row_top = row_boundaries[
            row_offset + 1
        ]
        row_bottom = row_boundaries[
            row_offset + 2
        ]
        row_index = row_offset + 1

        cells = crop_static_cells_for_row(
            table_image,
            static_layout,
            row_top,
            row_bottom,
        )

        has_static_content = (
            static_row_has_visual_content(
                cells
            )
        )
        has_blue_checks = (
            row_has_blue_check_content(
                table_image,
                check_boundaries,
                row_top,
                row_bottom,
            )
        )
        should_preserve = (
            has_static_content
            or has_blue_checks
        )

        try:
            # 只有真正空白的預印列可以跳過。
            if not should_preserve:
                raw_parts.append(
                    json.dumps(
                        {
                            "row": row_index,
                            "chosen": None,
                            "reason": (
                                "左側無固定內容且右側無藍筆，"
                                "判定為空白預印列"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            try:
                row, raw = read_one_static_row(
                    file_path,
                    page_number,
                    row_index,
                    cells,
                    force_preserve=has_blue_checks,
                )
            except Exception as error:
                # 只要影像證明這列有內容，Agent 錯誤也不能刪列。
                row = make_unclear_static_row_from_cells(
                    cells
                )
                raw = json.dumps(
                    {
                        "row": row_index,
                        "chosen": row,
                        "reason": "static_agent_failed_but_row_preserved",
                        "error": (
                            f"{type(error).__name__}: {error}"
                        ),
                    },
                    ensure_ascii=False,
                )
                errors.append(
                    f"第 {page_number} 頁"
                    f"第 {row_index} 個資料列"
                    "固定欄分欄辨識失敗，"
                    "已保留為 unclear："
                    f"{type(error).__name__}: "
                    f"{error}"
                )

            if raw:
                raw_parts.append(raw)

            if row is None:
                # should_preserve=True 時理論上不應發生；保守起見仍保留。
                row = make_unclear_static_row_from_cells(
                    cells
                )
                errors.append(
                    f"第 {page_number} 頁"
                    f"第 {row_index} 個資料列影像有內容，"
                    "但 Agent 回傳空列，已保留為 unclear"
                )

            row = canonicalize_static_row(
                row
            )

            # 影像門檻可能仍被掃描雜訊觸發。
            # 若模型最終確認「編號以外」沒有任何真正內容，
            # 且右側也沒有藍筆，這就是只有預印編號的空白列。
            if (
                not static_row_has_meaningful_content(
                    row
                )
                and not has_blue_checks
            ):
                raw_parts.append(
                    json.dumps(
                        {
                            "row": row_index,
                            "chosen": None,
                            "reason": (
                                "模型確認編號以外沒有有效固定內容，"
                                "且右側沒有藍筆，跳過空白預印列"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            row_id = normalize_row_id(
                row.get("編號")
            )

            if not is_valid_static_row_id(
                row.get("編號")
            ):
                errors.append(
                    f"第 {page_number} 頁"
                    f"第 {row_index} 個資料列有固定內容，"
                    "但編號無法確認，已保留為 unclear"
                )
            elif row_id in seen_ids:
                errors.append(
                    f"第 {page_number} 頁"
                    f"出現重複編號 {row_id}，"
                    "後一筆標記為人工確認"
                )
            else:
                seen_ids.add(row_id)

            rows.append(row)
            ranges.append(
                (
                    row_top,
                    row_bottom,
                )
            )

        finally:
            for cell in cells:
                cell.close()

    return rows, ranges, raw_parts


def read_generic_page(
    state: dict[str, Any],
) -> dict[str, Any]:
    print(f"[Generic Reader] 版本：{GENERIC_READER_VERSION}")
    file_path = state["file_path"]
    page_number = int(
        state["page_number"]
    )

    errors = list(
        state.get(
            "errors",
            [],
        )
    )

    header_raw = ""
    static_raw_parts: list[str] = []
    check_raw_parts: list[str] = []

    header_image: Image.Image | None = None
    row_image: Image.Image | None = None
    table_image: Image.Image | None = None

    try:
        rotation = get_cached_rotation(
            file_path,
            page_number,
        )

        cache_key = os.path.abspath(
            file_path
        )
        document_cache = (
            _DOCUMENT_CACHE.setdefault(
                cache_key,
                {},
            )
        )

        cached_headers = document_cache.get(
            "generic_headers"
        )
        cached_metadata = document_cache.get(
            "generic_metadata"
        )

        # ====================================================
        # Header / metadata：同一份文件只辨識一次
        # ====================================================
        if (
            isinstance(cached_headers, list)
            and cached_headers
        ):
            headers = list(
                cached_headers
            )
            metadata = dict(
                cached_metadata
                or {}
            )

            print(
                "[Generic Header] "
                f"使用文件快取 headers："
                f"{headers}"
            )

        else:
            header_image = render_page_image(
                file_path,
                page_number,
                rotation,
                HEADER_DPI,
            )

            header_prompt = """
你是通用檢查表 Header 與 Metadata 辨識器。

請讀取：
1. 公司名稱、日期、架刀人員、客戶、品名/圖號、
   Rev.、第二工程、機台編號、檢查時間。
2. 主要資料表真正的 headers。

主要表頭必須從「編號」開始。
不得把頁首說明、重要尺寸代號、備註、簽名欄、
越南文說明或頁尾當成 Header。

「首件」與 1、2、3……11 是不同欄位。

只輸出合法 JSON：
{
  "metadata": {
    "公司名稱": null,
    "日期": null,
    "架刀人員": null,
    "客戶": null,
    "品名/圖號": null,
    "Rev.": null,
    "第二工程": null,
    "機台編號": null,
    "檢查時間": null
  },
  "headers": [
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
    "11"
  ]
}
"""

            header_raw = call_vlm(
                header_prompt,
                [
                    image_to_base64(
                        header_image
                    )
                ],
                900,
            )

            metadata_new, detected_headers = (
                parse_header_result(
                    header_raw
                )
            )

            headers = clean_generic_headers(
                detected_headers
            )
            headers = clean_generic_headers(
                merge_router_headers(
                    headers,
                    get_router_headers(
                        file_path
                    ),
                )
            )

            if headers != STANDARD_HEADERS:
                normalized = {
                    normalize_key(header)
                    for header in headers
                }

                if (
                    normalize_key("編號")
                    in normalized
                    and normalize_key(
                        "檢驗標準"
                    ) in normalized
                ):
                    headers = list(
                        STANDARD_HEADERS
                    )

            if headers != STANDARD_HEADERS:
                raise ValueError(
                    "主要資料表 Header 無法標準化"
                )

            metadata = normalize_metadata(
                metadata_new
            )
            document_cache[
                "generic_headers"
            ] = list(headers)
            document_cache[
                "generic_metadata"
            ] = dict(metadata)

            print(
                "[Generic Header] "
                f"第二次辨識 headers："
                f"{detected_headers}"
            )
            print(
                "[Generic Header] "
                f"清理後 headers："
                f"{headers}"
            )

        # ====================================================
        # 高解析主表：先找格線，再讀內容
        # ====================================================
        row_image = render_page_image(
            file_path,
            page_number,
            rotation,
            ROW_DPI,
        )
        table_image = crop_main_table(
            row_image
        )

        check_boundaries, row_boundaries = (
            get_grid_first_boundaries(
                file_path,
                table_image,
            )
        )

        static_layout = get_static_column_layout(
            file_path,
            table_image,
            check_boundaries[0],
        )

        static_rows, static_ranges, static_raw_parts = (
            read_static_rows_grid_first(
                file_path,
                page_number,
                table_image,
                static_layout,
                check_boundaries,
                row_boundaries,
                errors,
            )
        )

        print(
            "[Generic Static] "
            f"保留 {len(static_rows)} 筆非空白實體資料列，"
            f"格線共 {len(row_boundaries) - 2} 列"
        )
        print(
            "[Generic Static] "
            f"本頁讀到的有效編號："
            f"{[row.get('編號') for row in static_rows]}"
        )

        # 空白頁允許沒有有效列，不再硬報錯。
        check_rows: list[
            dict[str, Any]
        ] = []

        for static_row, (
            row_top,
            row_bottom,
        ) in zip(
            static_rows,
            static_ranges,
        ):
            check_row, raw = (
                read_check_cells_for_row(
                    file_path,
                    page_number,
                    table_image,
                    check_boundaries,
                    row_top,
                    row_bottom,
                    static_row,
                    errors,
                )
            )
            check_rows.append(
                check_row
            )

            if raw:
                check_raw_parts.append(
                    raw
                )

        rows = merge_static_with_fixed_checks(
            static_rows,
            check_rows,
        )

        # 最終再強制一次固定 key，保證不會有「量具 編號」等欄位漂移。
        rows = [
            {
                header: normalize_cell_value(
                    {
                        normalize_key(key): value
                        for key, value in row.items()
                    }.get(
                        normalize_key(header)
                    )
                )
                for header in STANDARD_HEADERS
            }
            for row in rows
        ]

        old_metadata = (
            state.get("metadata")
            or {}
        )

        if old_metadata:
            merged_metadata = dict(
                normalize_metadata(
                    old_metadata
                )
            )
            merged_metadata.update(
                {
                    key: value
                    for key, value in (
                        metadata or {}
                    ).items()
                    if value is not None
                }
            )
            metadata = normalize_metadata(
                merged_metadata
            )
        else:
            metadata = normalize_metadata(
                metadata
            )

        document_cache[
            "generic_metadata"
        ] = dict(metadata)

        return {
            **state,
            "metadata": metadata,
            "headers": list(
                STANDARD_HEADERS
            ),
            "rows": rows,
            "metadata_raw_response": header_raw,
            "header_raw_response": header_raw,
            "row_raw_response": json.dumps(
                {
                    "static_raw_responses": (
                        static_raw_parts
                    ),
                    "cell_raw_responses": (
                        check_raw_parts
                    ),
                    "detected_row_slots": (
                        len(row_boundaries) - 2
                    ),
                    "populated_row_count": (
                        len(static_rows)
                    ),
                },
                ensure_ascii=False,
            ),
            "errors": errors,
        }

    except Exception as error:
        errors.append(
            "read_generic_page error: "
            f"{type(error).__name__}: "
            f"{error}"
        )

        cache = _DOCUMENT_CACHE.get(
            os.path.abspath(file_path),
            {},
        )

        return {
            **state,
            "metadata": normalize_metadata(
                state.get("metadata")
                or cache.get(
                    "generic_metadata",
                    {},
                )
            ),
            "headers": list(
                cache.get(
                    "generic_headers",
                    [],
                )
            ),
            "rows": [],
            "metadata_raw_response": header_raw,
            "header_raw_response": header_raw,
            "row_raw_response": json.dumps(
                {
                    "static_raw_responses": (
                        static_raw_parts
                    ),
                    "cell_raw_responses": (
                        check_raw_parts
                    ),
                },
                ensure_ascii=False,
            ),
            "errors": errors,
        }

    finally:
        for image in (
            header_image,
            table_image,
            row_image,
        ):
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass
