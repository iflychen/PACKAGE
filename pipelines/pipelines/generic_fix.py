from __future__ import annotations

import py_compile
import re
import shutil
import textwrap
from datetime import datetime
from pathlib import Path


TARGET_FILE = Path("generic_rowreader.py")


def replace_function(
    source: str,
    function_name: str,
    replacement: str,
) -> str:
    """
    取代一個最外層函式。

    從：
        def function_name(...)

    一直到下一個最外層 def、章節分隔線或檔案結尾。
    """

    pattern = re.compile(
        rf"(?ms)"
        rf"^def\s+{re.escape(function_name)}\s*\("
        rf".*?"
        rf"(?="
        rf"^def\s+"
        rf"|^# ={{20,}}"
        rf"|\Z"
        rf")"
    )

    match = pattern.search(source)

    if match is None:
        raise RuntimeError(
            f"找不到函式：{function_name}\n"
            "請確認目前使用的是逐橫列版本的 "
            "generic_rowreader.py。"
        )

    clean_replacement = (
        textwrap.dedent(replacement)
        .strip()
        + "\n\n"
    )

    return (
        source[:match.start()]
        + clean_replacement
        + source[match.end():]
    )


REPAIR_MODEL_JSON = r'''
def repair_model_json(text: str) -> str:
    """
    修復模型常見的非標準 JSON。

    例如：

        "R5": unclear

    會修成：

        "R5": "unclear"

    單一的勾號、叉號、加號或減號，
    若模型忘記加引號，也一併修復。
    """

    text = re.sub(
        r"\\u0?51\.1",
        "ø5.1",
        text,
        flags=re.IGNORECASE,
    )

    # "R5": unclear
    # 改成：
    # "R5": "unclear"
    text = re.sub(
        r'(:\s*)unclear(?=\s*[,}\]])',
        r'\1"unclear"',
        text,
        flags=re.IGNORECASE,
    )

    # clear 也必須是 JSON 字串。
    text = re.sub(
        r'(:\s*)clear(?=\s*[,}\]])',
        r'\1"clear"',
        text,
        flags=re.IGNORECASE,
    )

    # 未加引號的勾號或叉號。
    text = re.sub(
        r'(:\s*)(✓|✔|✗|×)(?=\s*[,}\]])',
        r'\1"\2"',
        text,
    )

    # 未加引號的單一加號或減號。
    text = re.sub(
        r'(:\s*)([+-])(?=\s*[,}\]])',
        r'\1"\2"',
        text,
    )

    # Python 形式的 None / True / False。
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

    # 修復非法反斜線。
    return re.sub(
        r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})',
        r"\\\\",
        text,
    )
'''


GET_CHECK_MATRIX_BOUNDARIES = r'''
def get_check_matrix_boundaries(
    table: Image.Image,
    item_count: int,
) -> tuple[
    list[int],
    list[int],
]:
    """
    自動尋找右側檢查矩陣。

    通用定位邏輯：

    1. 找出所有垂直格線。
    2. 從最右邊開始檢查相鄰兩條垂直線。
    3. 找到包含 1～11 的最右側窄標籤欄。
    4. 使用標籤欄左邊界作為資料矩陣右邊界。
    5. 從該邊界往左尋找 item_count 個等距項目格。

    不使用固定 47% 或其他單一版型比例。
    """

    vertical_lines = sorted(
        find_vertical_grid_lines(
            table
        )
    )

    check_count = len(
        CHECK_MATRIX_HEADERS
    )

    label_candidates: list[
        tuple[
            float,
            int,
            int,
            list[int],
        ]
    ] = []

    # --------------------------------------------------------
    # A. 從最右側開始尋找 1～11 標籤欄
    # --------------------------------------------------------
    for index in range(
        len(vertical_lines) - 1,
        0,
        -1,
    ):
        label_left = vertical_lines[
            index - 1
        ]

        label_right = vertical_lines[
            index
        ]

        label_width = (
            label_right
            - label_left
        )

        width_ratio = (
            label_width
            / max(
                table.width,
                1,
            )
        )

        # 標籤欄通常是窄欄。
        # 範圍刻意放寬以支援不同版型。
        if not (
            0.008
            <= width_ratio
            <= 0.15
        ):
            continue

        inner_left = min(
            label_right - 1,
            label_left + 2,
        )

        inner_right = max(
            inner_left + 1,
            label_right - 2,
        )

        label_image = table.crop(
            (
                inner_left,
                0,
                inner_right,
                table.height,
            )
        )

        try:
            label_horizontal_lines = (
                find_horizontal_grid_lines(
                    label_image
                )
            )

            label_check_boundaries = (
                select_matrix_check_boundaries(
                    label_horizontal_lines,
                    check_count,
                    label_image.height,
                )
            )

            if not label_check_boundaries:
                continue

            # 估計欄內是否真的有文字。
            gray = label_image.convert("L")

            try:
                pixels = gray.tobytes()

                dark_count = sum(
                    1
                    for pixel in pixels
                    if pixel < 180
                )

                ink_ratio = (
                    dark_count
                    / max(
                        len(pixels),
                        1,
                    )
                )

            finally:
                gray.close()

            right_margin_ratio = (
                table.width
                - label_right
            ) / max(
                table.width,
                1,
            )

            boundary_gaps = [
                label_check_boundaries[
                    gap_index + 1
                ]
                - label_check_boundaries[
                    gap_index
                ]
                for gap_index in range(
                    len(
                        label_check_boundaries
                    )
                    - 1
                )
            ]

            middle_gap = float(
                median(
                    boundary_gaps
                )
            )

            if middle_gap <= 0:
                continue

            deviation = sum(
                abs(
                    gap
                    - middle_gap
                )
                for gap in boundary_gaps
            ) / (
                len(boundary_gaps)
                * middle_gap
            )

            # 越靠右、水平格線越規律、
            # 欄內有少量數字筆跡，分數越低。
            score = (
                right_margin_ratio
                * 10.0
                + deviation
                * 5.0
                + abs(
                    width_ratio
                    - 0.04
                )
                + (
                    0.20
                    if ink_ratio < 0.001
                    else 0.0
                )
            )

            label_candidates.append(
                (
                    score,
                    label_left,
                    label_right,
                    label_check_boundaries,
                )
            )

        finally:
            label_image.close()

    if label_candidates:
        label_candidates.sort(
            key=lambda item: item[0]
        )

        (
            _,
            label_left,
            label_right,
            check_boundaries,
        ) = label_candidates[0]

        print(
            "[Generic Matrix] "
            "找到最右側 1～11 標籤欄："
            f"{label_left}:{label_right}"
        )

    else:
        # ----------------------------------------------------
        # B. 找不到標籤欄時的通用備援
        # ----------------------------------------------------
        print(
            "[Generic Matrix] "
            "找不到可靠的 1～11 標籤欄，"
            "改用右側格線備援"
        )

        if len(vertical_lines) >= 2:
            label_left = vertical_lines[-2]
            label_right = vertical_lines[-1]
        else:
            label_left = int(
                table.width
                * MATRIX_FALLBACK_RIGHT_RATIO
            )

            label_right = table.width

        horizontal_lines = (
            find_horizontal_grid_lines(
                table
            )
        )

        check_boundaries = (
            select_matrix_check_boundaries(
                horizontal_lines,
                check_count,
                table.height,
            )
        )

        if not check_boundaries:
            top = int(
                table.height
                * MATRIX_FALLBACK_TOP_RATIO
            )

            bottom = int(
                table.height
                * MATRIX_FALLBACK_BOTTOM_RATIO
            )

            check_boundaries = (
                build_equal_boundaries(
                    top,
                    bottom,
                    check_count,
                )
            )

    # --------------------------------------------------------
    # C. 以標籤欄左邊界鎖定資料矩陣右邊界
    # --------------------------------------------------------
    required_item_boundaries = (
        item_count
        + 1
    )

    anchor_tolerance = max(
        5,
        int(
            table.width
            * 0.008
        ),
    )

    available_lines = [
        line
        for line in vertical_lines
        if line
        <= label_left
        + anchor_tolerance
    ]

    item_candidates: list[
        tuple[
            float,
            list[int],
        ]
    ] = []

    for start in range(
        0,
        len(available_lines)
        - required_item_boundaries
        + 1,
    ):
        sequence = available_lines[
            start:
            start
            + required_item_boundaries
        ]

        gaps = [
            sequence[
                gap_index + 1
            ]
            - sequence[
                gap_index
            ]
            for gap_index in range(
                len(sequence)
                - 1
            )
        ]

        if not gaps:
            continue

        middle_gap = float(
            median(gaps)
        )

        if middle_gap < 5:
            continue

        deviation = sum(
            abs(
                gap
                - middle_gap
            )
            for gap in gaps
        ) / (
            len(gaps)
            * middle_gap
        )

        anchor_distance = abs(
            sequence[-1]
            - label_left
        )

        # 最後一條線必須靠近標籤欄左邊界。
        if anchor_distance > max(
            anchor_tolerance,
            middle_gap * 0.65,
        ):
            continue

        smallest_gap = min(gaps)
        largest_gap = max(gaps)

        gap_range_penalty = (
            largest_gap
            - smallest_gap
        ) / middle_gap

        score = (
            deviation
            * 8.0
            + anchor_distance
            / middle_gap
            + gap_range_penalty
            * 0.5
        )

        item_candidates.append(
            (
                score,
                sequence,
            )
        )

    if item_candidates:
        item_candidates.sort(
            key=lambda item: item[0]
        )

        item_boundaries = list(
            item_candidates[0][1]
        )

        # 將最後邊界精確對齊標籤欄左邊界。
        if abs(
            item_boundaries[-1]
            - label_left
        ) <= anchor_tolerance:
            item_boundaries[-1] = (
                label_left
            )

        print(
            "[Generic Matrix] "
            "由標籤欄反推項目範圍："
            f"{item_boundaries[0]}:"
            f"{item_boundaries[-1]}"
        )

    else:
        # ----------------------------------------------------
        # D. 找不到完整垂直線時，依附近線距等距反推
        # ----------------------------------------------------
        nearby_lines = [
            line
            for line in vertical_lines
            if line < label_left
        ]

        nearby_gaps = [
            nearby_lines[index + 1]
            - nearby_lines[index]
            for index in range(
                max(
                    0,
                    len(nearby_lines)
                    - 10,
                ),
                len(nearby_lines)
                - 1,
            )
        ]

        nearby_gaps = [
            gap
            for gap in nearby_gaps
            if gap > 4
        ]

        if nearby_gaps:
            estimated_gap = float(
                median(
                    nearby_gaps
                )
            )

            estimated_left = int(
                round(
                    label_left
                    - item_count
                    * estimated_gap
                )
            )

        else:
            estimated_left = int(
                table.width
                * MATRIX_FALLBACK_LEFT_RATIO
            )

        estimated_left = max(
            0,
            min(
                estimated_left,
                label_left - 1,
            ),
        )

        item_boundaries = (
            build_equal_boundaries(
                estimated_left,
                label_left,
                item_count,
            )
        )

        print(
            "[Generic Matrix] "
            "項目垂直線不足，"
            "由標籤欄向左等距反推："
            f"{estimated_left}:"
            f"{label_left}"
        )

    return (
        item_boundaries,
        check_boundaries,
    )
'''


READ_ONE_HORIZONTAL_CHECK_STRIP = r'''
def read_one_horizontal_check_strip(
    file_path: str,
    page_number: int,
    check_header: str,
    table: Image.Image,
    item_boundaries: list[int],
    y0: int,
    y1: int,
    visual_rows: list[
        dict[str, Any]
    ],
) -> dict[str, Any]:
    """
    辨識一條指定的橫列，例如欄位 1、2 或 3。

    此函式內部會攔截：

    - Ollama HTTP 錯誤
    - 非法 JSON
    - 單一橫列其他辨識錯誤

    某一條橫列失敗時，只讓該橫列維持 null，
    不會讓整頁 headers 與 rows 全部被清空。
    """

    output: dict[str, Any] = {
        normalize_row_id(
            row.get("編號")
        ): None
        for row in visual_rows
        if normalize_row_id(
            row.get("編號")
        )
    }

    cells: dict[
        str,
        Image.Image,
    ] = {}

    label_information: dict[
        str,
        dict[str, Any],
    ] = {}

    contact_sheet: Image.Image | None = None

    try:
        for visual_index, static_row in enumerate(
            visual_rows
        ):
            if (
                visual_index + 1
                >= len(item_boundaries)
            ):
                break

            x0 = item_boundaries[
                visual_index
            ]

            x1 = item_boundaries[
                visual_index + 1
            ]

            cell = crop_inner_cell(
                table,
                x0,
                y0,
                x1,
                y1,
            )

            row_id = str(
                static_row.get("編號")
                or ""
            ).strip()

            normalized_id = (
                normalize_row_id(
                    row_id
                )
            )

            if not normalized_id:
                cell.close()
                continue

            if not cell_has_ink(cell):
                cell.close()
                continue

            label = f"R{visual_index}"

            cells[label] = cell

            label_information[label] = {
                "編號": row_id,
                "量具編號": (
                    static_row.get(
                        "量具編號"
                    )
                ),
                "檢驗標準": (
                    static_row.get(
                        "檢驗標準"
                    )
                ),
                "公差": (
                    static_row.get(
                        "公差"
                    )
                ),
                "類型": (
                    infer_check_value_type(
                        static_row
                    )
                ),
            }

        if not cells:
            return output

        contact_sheet = (
            build_horizontal_strip_contact_sheet(
                cells
            )
        )

        save_debug_image(
            file_path,
            page_number,
            f"check_strip_{check_header}",
            contact_sheet,
        )

        prompt = f"""
你現在只辨識右側檢查矩陣的第 {check_header} 橫列。

圖片中的每個小框左上角有 R0、R1 等標籤。

每個 R 標籤對應資料：

{json.dumps(
    label_information,
    ensure_ascii=False,
    indent=2,
)}

嚴格規則：

1. 現在固定讀的是欄位「{check_header}」。
   不需要判斷欄位名稱。

2. 每個 R 標籤只代表一個產品編號。

3. 不得把一個 R 格子的內容移給另一個 R。

4. 類型為 "number"：
   只允許數字、括號數字、"unclear" 或 null。
   禁止輸出勾號與叉號。
   必須保留小數點。

5. 類型為 "check"：
   只允許 "✓"、"✗"、"unclear" 或 null。
   U、V、弧形與單筆彎曲記號通常是 "✓"。
   只有清楚交叉成 X 才是 "✗"。

6. 所有文字值都必須使用雙引號。
   正確：
       "R1": "unclear"

   錯誤：
       "R1": unclear

7. 看不清楚時輸出 "unclear"，
   不得猜測。

8. 只輸出圖片中存在的 R 標籤。

9. 只輸出合法 JSON。

輸出：

{{
  "cells": {{
    "R0": null
  }}
}}
"""

        try:
            raw = call_vlm(
                prompt,
                [
                    image_to_base64(
                        contact_sheet
                    )
                ],
                600,
            )

            data = parse_json_response(
                raw,
                (
                    "Generic Horizontal Strip Agent "
                    f"{check_header}"
                ),
            )

        except Exception as error:
            print(
                "[Generic Matrix] "
                f"橫列 {check_header} 辨識失敗，"
                "該橫列保留 null，"
                "繼續讀取下一條橫列。"
            )

            print(
                "[Generic Matrix] "
                f"{type(error).__name__}: "
                f"{error}"
            )

            return output

        model_cells: dict[
            str,
            Any,
        ] = {}

        if isinstance(data, dict):
            candidate = data.get(
                "cells",
                {},
            )

            if isinstance(
                candidate,
                dict,
            ):
                model_cells = candidate

        for label, information in (
            label_information.items()
        ):
            row_id = normalize_row_id(
                information.get("編號")
            )

            value_type = str(
                information.get("類型")
                or "check"
            )

            output[row_id] = (
                validate_cell_value(
                    model_cells.get(label),
                    value_type,
                )
            )

        return output

    except Exception as error:
        # 即使裁切、contact sheet 或其他局部流程失敗，
        # 也不能讓整頁失敗。
        print(
            "[Generic Matrix] "
            f"橫列 {check_header} "
            "局部處理失敗，保留 null："
            f"{type(error).__name__}: "
            f"{error}"
        )

        return output

    finally:
        for cell in cells.values():
            try:
                cell.close()
            except Exception:
                pass

        if contact_sheet is not None:
            try:
                contact_sheet.close()
            except Exception:
                pass
'''


def patch_process_alias(
    source: str,
) -> str:
    """
    上一輪輸出已讀到「第二工程」，
    但 Neon 尋找「製程」。

    這只增加欄位別名，不改 OCR 邏輯。
    """

    if (
        'output["製程"]'
        in source
    ):
        return source

    pattern = re.compile(
        r'''(?ms)
        (
            if\s+part_number\s+is\s+not\s+None:
            \s*
            output\["品號"\]\s*=\s*part_number
        )
        \s*
        return\s+output
        ''',
        flags=re.VERBOSE,
    )

    replacement = r'''
\1

    process_name = (
        output.get("製程")
        or output.get("第二工程")
    )

    if process_name is not None:
        output["製程"] = process_name

    return output
'''

    updated, count = pattern.subn(
        replacement,
        source,
        count=1,
    )

    if count == 0:
        print(
            "警告：找不到 normalize_metadata "
            "中的品號區塊，未自動加入製程別名。"
        )

        return source

    return updated


def main() -> None:
    if not TARGET_FILE.is_file():
        raise FileNotFoundError(
            f"找不到：{TARGET_FILE.resolve()}\n"
            "請把本程式放在 "
            "generic_rowreader.py 同一個資料夾。"
        )

    original_source = (
        TARGET_FILE.read_text(
            encoding="utf-8"
        )
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_file = TARGET_FILE.with_name(
        f"{TARGET_FILE.stem}_backup_"
        f"{timestamp}"
        f"{TARGET_FILE.suffix}"
    )

    shutil.copy2(
        TARGET_FILE,
        backup_file,
    )

    updated_source = original_source

    updated_source = replace_function(
        updated_source,
        "repair_model_json",
        REPAIR_MODEL_JSON,
    )

    updated_source = replace_function(
        updated_source,
        "get_check_matrix_boundaries",
        GET_CHECK_MATRIX_BOUNDARIES,
    )

    updated_source = replace_function(
        updated_source,
        "read_one_horizontal_check_strip",
        READ_ONE_HORIZONTAL_CHECK_STRIP,
    )

    #updated_source = patch_process_alias(
     #   updated_source
    #)

    TARGET_FILE.write_text(
        updated_source,
        encoding="utf-8",
    )

    try:
        py_compile.compile(
            str(TARGET_FILE),
            doraise=True,
        )

    except Exception:
        # 語法錯誤時自動還原原始檔。
        shutil.copy2(
            backup_file,
            TARGET_FILE,
        )

        raise RuntimeError(
            "修改後語法檢查失敗，"
            "已自動還原原本的 "
            "generic_rowreader.py。"
        )

    print()
    print(
        "修改完成："
        f"{TARGET_FILE.resolve()}"
    )

    print(
        "原始備份："
        f"{backup_file.resolve()}"
    )

    print()
    print(
        "已套用："
    )

    print(
        "1. 自動尋找最右側 1～11 標籤欄"
    )

    print(
        "2. 由標籤欄往左反推資料矩陣"
    )

    print(
        '3. 自動修復裸的 unclear'
    )

    print(
        "4. 單一橫列失敗不再中止整頁"
    )

    print(
        "5. 第二工程同步提供製程欄位"
    )


if __name__ == "__main__":
    main()