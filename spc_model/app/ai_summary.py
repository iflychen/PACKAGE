from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.llm import generate_with_ollama


class AiSummaryRequest(BaseModel):      # data direct from dashboard
    chart_data: dict[str, Any]
    capability_data: dict[str, Any] | None = None


class AiSummaryResponse(BaseModel):
    summary: str
    summary_context: str


# ============================================================================
#  設計原則
#  ---------------------------------------------------------------------------
#  7B 等級的本機模型很會「寫句子」，但不擅長「判斷」：
#    - 把違反 Western / Nelson 連串規則的點說成「超出管制界限」
#    - Cp 2.05、Cpk 2.05 卻說製程偏心，再建議「校正製程中心」
#    - 逐一列出五個能力指標，每句都重複同樣的話
#  所以所有判斷都在這裡用程式算好，寫成「系統判讀」交給模型，
#  模型只負責把結論整理成通順的中文，不再自己下判斷。
# ============================================================================

SPEC_RULES = {"above_usl", "below_lsl"}
LIMIT_RULES = {"above_ucl", "below_lcl"}
UPWARD_RULES = {
    "nelson_8_points_above_center",
    "western_4_of_5_above_1sigma",
    "western_2_of_3_above_2sigma",
}
DOWNWARD_RULES = {
    "nelson_8_points_below_center",
    "western_4_of_5_below_1sigma",
    "western_2_of_3_below_2sigma",
}

RULE_LABELS = {
    "above_usl": "超出規格上限 USL",
    "below_lsl": "低於規格下限 LSL",
    "above_ucl": "超出管制上限 UCL",
    "below_lcl": "低於管制下限 LCL",
    "western_2_of_3_above_2sigma": "連續 3 點中有 2 點高於 +2σ",
    "western_2_of_3_below_2sigma": "連續 3 點中有 2 點低於 -2σ",
    "western_4_of_5_above_1sigma": "連續 5 點中有 4 點高於 +1σ",
    "western_4_of_5_below_1sigma": "連續 5 點中有 4 點低於 -1σ",
    "nelson_8_points_above_center": "連續 8 點在中心線上方",
    "nelson_8_points_below_center": "連續 8 點在中心線下方",
}

COMPONENT_LABELS = {
    "I": "I 圖（個別值）",
    "XBAR": "Xbar 圖（子組平均）",
    "MR": "MR 圖（移動全距）",
    "R": "R 圖（子組全距）",
    "S": "S 圖（子組標準差）",
}

# Cpk / Cp 低於這個比例才視為製程中心偏離規格中心（k > 0.1）。
CENTERING_RATIO = 0.9


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def _fmt(value: Any, digits: int = 4) -> str:
    number = _num(value)
    if number is None:
        return "未提供"
    return f"{number:.{digits}f}"


def _point_label(point: dict[str, Any]) -> str:
    return f"#{point.get('x')}"


def _compress_ids(points: list[dict[str, Any]]) -> str:
    """把 #21、#22、#23 壓成 #21–#23，讓模型不用自己數。"""
    labels = [str(point.get("x")) for point in points]
    if not labels:
        return "無"
    if not all(label.isdigit() for label in labels):
        return "、".join(f"#{label}" for label in labels)

    numbers = sorted({int(label) for label in labels})
    parts: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:  # type: ignore[list-item]
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(f"#{start}" if start == previous else f"#{start}–#{previous}")
        if number is not None:
            start = previous = number
    return "、".join(parts)


def _classify_points(points: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    依 violated_rules 分成三種，彼此可重疊：
      out_of_spec   超出 USL / LSL
      beyond_limits 超出 UCL / LCL
      run_rule_only 只違反連串規則，點本身仍在 UCL / LCL 內
    """
    out_of_spec: list[dict[str, Any]] = []
    beyond_limits: list[dict[str, Any]] = []
    run_rule_only: list[dict[str, Any]] = []
    abnormal: list[dict[str, Any]] = []

    for point in points:
        rules = {str(rule) for rule in point.get("violated_rules") or []}
        is_spec = point.get("is_out_of_spec") is True or bool(rules & SPEC_RULES)
        is_limit = bool(rules & LIMIT_RULES)
        is_control = point.get("is_out_of_control") is True

        if is_spec:
            out_of_spec.append(point)
        if is_limit:
            beyond_limits.append(point)
        elif is_control:
            run_rule_only.append(point)
        if is_spec or is_control:
            abnormal.append(point)

    return {
        "out_of_spec": out_of_spec,
        "beyond_limits": beyond_limits,
        "run_rule_only": run_rule_only,
        "abnormal": abnormal,
    }


def _has_limits(limits: dict[str, Any]) -> bool:
    return _num(limits.get("ucl")) is not None and _num(limits.get("lcl")) is not None


def _with_rule(points: list[dict[str, Any]], rule: str) -> list[dict[str, Any]]:
    return [
        point
        for point in points
        if rule in {str(item) for item in point.get("violated_rules") or []}
    ]


def _by_direction(
    points: list[dict[str, Any]],
    upper_rule: str,
    lower_rule: str,
    upper_label: str,
    lower_label: str,
) -> str:
    """寫成「#30 超出 UCL」，不要讓模型看到「UCL/LCL」這種要自己判斷方向的寫法。"""
    parts: list[str] = []
    upper = _with_rule(points, upper_rule)
    lower = _with_rule(points, lower_rule)
    if upper:
        parts.append(f"{_compress_ids(upper)} {upper_label}")
    if lower:
        parts.append(f"{_compress_ids(lower)} {lower_label}")
    return "；".join(parts) or "無"


def _chart_section(
    title: str,
    series: dict[str, Any],
    *,
    with_spec: bool,
    detail_limit: int = 12,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    points = [p for p in series.get("points") or [] if isinstance(p, dict)]
    points = [p for p in points if _num(p.get("value")) is not None]
    limits = series.get("limits") if isinstance(series.get("limits"), dict) else {}
    groups = _classify_points(points)

    lines = [f"【{title}】共 {len(points)} 點"]
    limit_text = (
        f"UCL {_fmt(limits.get('ucl'))}、CL {_fmt(limits.get('cl'))}、LCL {_fmt(limits.get('lcl'))}"
        if _has_limits(limits)
        else "尚未建立管制界線"
    )
    lines.append(f"- 管制界線：{limit_text}")
    if with_spec:
        lines.append(
            f"- 規格界線：USL {_fmt(limits.get('usl'))}、LSL {_fmt(limits.get('lsl'))}"
        )
        lines.append(
            f"- 超出規格：{len(groups['out_of_spec'])} 點（"
            + _by_direction(
                groups["out_of_spec"], "above_usl", "below_lsl", "超出 USL", "低於 LSL"
            )
            + "）"
        )
    lines.append(
        f"- 超出管制界限：{len(groups['beyond_limits'])} 點（"
        + _by_direction(
            groups["beyond_limits"], "above_ucl", "below_lcl", "超出 UCL", "低於 LCL"
        )
        + "）"
    )
    lines.append(
        f"- 違反連串規則、但仍在管制界限內：{len(groups['run_rule_only'])} 點"
        f"（{_compress_ids(groups['run_rule_only'])}）"
    )
    lines.append(f"- 正常：{len(points) - len(groups['abnormal'])} 點")

    # 依規則整理點號，比逐點列出短，模型也比較不會照抄成一長串。
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for point in groups["abnormal"]:
        for rule in point.get("violated_rules") or []:
            by_rule.setdefault(str(rule), []).append(point)
    if by_rule:
        lines.append("- 依規則整理：")
        ordered = [rule for rule in RULE_LABELS if rule in by_rule]
        ordered += [rule for rule in by_rule if rule not in RULE_LABELS]
        for rule in ordered:
            lines.append(f"  {RULE_LABELS.get(rule, rule)}：{_compress_ids(by_rule[rule])}")

    # 只有超出界限的點附上數值，連串規則的點數值意義不大。
    flagged = [
        point
        for point in groups["abnormal"]
        if point in groups["beyond_limits"] or point in groups["out_of_spec"]
    ]
    for point in flagged[:detail_limit]:
        lines.append(f"- {_point_label(point)} 數值：{_fmt(point.get('value'))}")

    return lines, groups


def _first_point(groups: dict[str, list[dict[str, Any]]], rule_set: set[str]) -> str:
    """第一個觸發指定規則的點，例如平均值偏移的起點 #21。"""
    for point in groups["abnormal"]:
        if rule_set & {str(rule) for rule in point.get("violated_rules") or []}:
            return _point_label(point)
    return ""


def _pattern_hints(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
    """依觸發的規則給出標準 SPC 解讀，避免模型自由聯想。"""
    hints: list[str] = []
    up = _first_point(groups, UPWARD_RULES)
    down = _first_point(groups, DOWNWARD_RULES)
    if up:
        hints.append(
            f"從 {up} 起多點持續落在中心線上方，"
            "代表製程平均值往上偏移（不是單點雜訊）"
        )
    if down:
        hints.append(
            f"從 {down} 起多點持續落在中心線下方，"
            "代表製程平均值往下偏移（不是單點雜訊）"
        )
    above_ucl = _with_rule(groups["beyond_limits"], "above_ucl")
    below_lcl = _with_rule(groups["beyond_limits"], "below_lcl")
    if above_ucl:
        hints.append(
            f"{_compress_ids(above_ucl)} 超出管制上限 UCL，代表出現特殊原因造成的變異"
        )
    if below_lcl:
        hints.append(
            f"{_compress_ids(below_lcl)} 低於管制下限 LCL，代表出現特殊原因造成的變異"
        )
    above_usl = _with_rule(groups["out_of_spec"], "above_usl")
    below_lsl = _with_rule(groups["out_of_spec"], "below_lsl")
    if above_usl:
        hints.append(
            f"{_compress_ids(above_usl)} 超出規格上限 USL，屬於不良品，需要隔離並確認處置"
        )
    if below_lsl:
        hints.append(
            f"{_compress_ids(below_lsl)} 低於規格下限 LSL，屬於不良品，需要隔離並確認處置"
        )
    return hints


# 下圖超出 UCL 時，各種圖代表的意義
SECONDARY_MEANING = {
    "MR": "相鄰兩筆量測值之間的變動",
    "R": "子組內的全距",
    "S": "子組內的標準差",
}


def _secondary_hints(
    component: Any,
    groups: dict[str, list[dict[str, Any]]],
) -> list[str]:
    meaning = SECONDARY_MEANING.get(str(component), "變異")
    hints: list[str] = []
    above = _with_rule(groups["beyond_limits"], "above_ucl")
    below = _with_rule(groups["beyond_limits"], "below_lcl")
    if above:
        hints.append(
            f"{_compress_ids(above)} 超出管制上限 UCL，代表{meaning}突然變大"
        )
    if below:
        hints.append(
            f"{_compress_ids(below)} 低於管制下限 LCL，代表{meaning}異常變小"
        )
    if groups["run_rule_only"]:
        hints.append(f"{_compress_ids(groups['run_rule_only'])} 違反連串規則")
    return hints


def _capability_section(
    capability_data: dict[str, Any] | None,
    *,
    chart_point_count: int,
    in_control: bool | None,
) -> list[str]:
    if not isinstance(capability_data, dict):
        return ["【製程能力】未提供，不可推測任何能力指標"]

    metrics = capability_data.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    table_summary = capability_data.get("table_summary")
    if not isinstance(table_summary, dict):
        table_summary = {}

    cp = _num(metrics.get("cp"))
    cpk = _num(metrics.get("cpk"))
    cpm = _num(metrics.get("cpm"))
    cpmk = _num(metrics.get("cpmk"))
    ppk = _num(metrics.get("ppk"))
    mean = _num(metrics.get("mean"))
    usl = _num(metrics.get("usl"))
    lsl = _num(metrics.get("lsl"))
    nominal = _num(metrics.get("nominal_value"))
    sample_size = _num(metrics.get("sample_size"))
    threshold = _num(capability_data.get("cpk_threshold"))

    lines = ["【製程能力】"]
    if cpk is None and cp is None:
        lines.append("- 能力指標未提供（可能樣本不足），不可推測")
        return lines

    lines.append(
        f"- 指標：Cp {_fmt(cp, 2)}、Cpk {_fmt(cpk, 2)}、Cpm {_fmt(cpm, 2)}、"
        f"Cpmk {_fmt(cpmk, 2)}、Ppk {_fmt(ppk, 2)}"
    )
    lines.append(
        f"- 平均 {_fmt(mean)}、標準差 {_fmt(metrics.get('sigma'))}、"
        f"目標值（定義值）{_fmt(nominal)}"
    )
    if sample_size is not None:
        n = int(sample_size)
        if chart_point_count and n < chart_point_count:
            lines.append(
                f"- 計算樣本：{n} 筆（管制圖共 {chart_point_count} 點，"
                "資料庫已標記為異常的樣本不納入能力計算）"
            )
        else:
            lines.append(f"- 計算樣本：{n} 筆")

    lines.append("系統判讀（已算好，必須照用；以下每一行都是可以直接引用的完整句子）：")
    if cpk is not None and threshold is not None:
        if cpk >= threshold:
            lines.append(f"- Cpk 為 {cpk:.2f}，高於門檻 {threshold:.2f}，達到門檻。")
        else:
            lines.append(
                f"- Cpk 為 {cpk:.2f}，低於門檻 {threshold:.2f}，製程能力不足。"
            )
        lines.append(
            "- （寫作限制，不要寫進摘要）門檻只適用於 Cpk；"
            "Cp、Cpm、Cpmk、Ppk 沒有門檻，不可說它們達標或未達標。"
        )

    if cp is not None and cpk is not None and cp > 0:
        ratio = cpk / cp
        if ratio >= CENTERING_RATIO:
            lines.append(
                f"- Cpk 與 Cp 幾乎相同（Cpk/Cp = {ratio:.2f}），計算樣本的平均接近規格中心，"
                "不需要為了提高 Cpk 而調整製程中心。"
            )
        else:
            side = ""
            if mean is not None and usl is not None and lsl is not None:
                side = "偏向規格上限" if mean > (usl + lsl) / 2 else "偏向規格下限"
            lines.append(
                f"- Cpk 明顯低於 Cp（Cpk/Cp = {ratio:.2f}），製程平均{side or '偏離規格中心'}，"
                "建議調整製程中心。"
            )

    if cp is not None and cpm is not None and cp > 0 and nominal is not None:
        if cpm / cp >= CENTERING_RATIO:
            lines.append(f"- Cpm 與 Cp 相近，平均接近目標值 {nominal:g}。")
        else:
            lines.append(
                f"- Cpm 明顯低於 Cp（Cpm/Cp = {cpm / cp:.2f}），平均偏離目標值 {nominal:g}。"
            )

    if cpk is not None and ppk is not None and cpk > 0:
        if ppk / cpk >= CENTERING_RATIO:
            lines.append("- Ppk 與 Cpk 相近，長期變異沒有明顯放大。")
        else:
            lines.append(
                f"- Ppk 明顯低於 Cpk（Ppk/Cpk = {ppk / cpk:.2f}），長期變異大於短期變異。"
            )

    if in_control is False:
        lines.append(
            "- 製程目前出現失控訊號，能力指標只代表正常樣本的表現，僅供參考，"
            "不可據此判定製程穩定。"
        )

    below = table_summary.get("below_threshold_features")
    if isinstance(below, list) and below:
        names = "、".join(
            f"{item.get('feature_name')}（Cpk {_fmt(item.get('cpk'), 2)}）"
            for item in below
            if isinstance(item, dict)
        )
        lines.append(f"- 同機台其他低於門檻的尺寸：{names}")

    return lines


def _recommendations(
    *,
    phase_ii: bool,
    primary_groups: dict[str, list[dict[str, Any]]],
    secondary_groups: dict[str, list[dict[str, Any]]] | None,
    secondary_component: Any,
    capability_data: dict[str, Any] | None,
) -> list[str]:
    """
    建議處置也由程式決定要寫哪幾點，避免模型漏掉偏移起點或亂建議調整中心。
    模型只負責把這幾點寫順，最多再補一點。
    """
    recs: list[str] = []

    if not phase_ii:
        recs.append("累積足夠樣本後，執行 Phase I 試算並核准管制界線，才能判斷製程是否穩定。")

    up = _first_point(primary_groups, UPWARD_RULES)
    down = _first_point(primary_groups, DOWNWARD_RULES)
    shifts = []
    if up:
        shifts.append(f"從 {up} 起製程平均值往上偏移")
    if down:
        shifts.append(f"從 {down} 起製程平均值往下偏移")
    beyond = primary_groups["beyond_limits"]

    if shifts and beyond:
        recs.append(
            f"調查{'、'.join(shifts)}，以及 {_compress_ids(beyond)} 超出管制界限的原因，"
            "原因排除後再確認管制圖回到正常。"
        )
    elif shifts:
        recs.append(f"調查{'、'.join(shifts)}的原因，原因排除後再確認管制圖回到正常。")
    elif beyond:
        recs.append(f"調查 {_compress_ids(beyond)} 超出管制界限的特殊原因。")
    elif primary_groups["run_rule_only"]:
        recs.append(
            f"調查 {_compress_ids(primary_groups['run_rule_only'])} 違反連串規則的原因。"
        )

    if primary_groups["out_of_spec"]:
        recs.append(
            f"隔離並確認 {_compress_ids(primary_groups['out_of_spec'])} 工件的處置，"
            "避免不良品流到下一道工序。"
        )

    primary_out_of_control = bool(beyond or primary_groups["run_rule_only"])
    if secondary_groups is not None and not primary_out_of_control:
        secondary_points = secondary_groups["beyond_limits"] + secondary_groups["run_rule_only"]
        if secondary_points:
            meaning = SECONDARY_MEANING.get(str(secondary_component), "變異")
            recs.append(f"調查 {_compress_ids(secondary_points)} {meaning}異常的原因。")

    if isinstance(capability_data, dict) and isinstance(capability_data.get("metrics"), dict):
        metrics = capability_data["metrics"]
        cp = _num(metrics.get("cp"))
        cpk = _num(metrics.get("cpk"))
        threshold = _num(capability_data.get("cpk_threshold"))
        off_center = cp is not None and cpk is not None and cp > 0 and cpk / cp < CENTERING_RATIO
        if off_center:
            recs.append("Cpk 明顯低於 Cp，製程平均偏離規格中心，建議調整製程中心。")
        elif cpk is not None and threshold is not None and cpk < threshold:
            recs.append("Cpk 低於門檻，需要降低製程變異以提升製程能力。")

    if not recs:
        recs.append("製程目前沒有異常訊號，維持現行監控即可。")
    return recs[:4]


def build_summary_context(
    chart_data: dict[str, Any],
    capability_data: dict[str, Any] | None = None,
) -> str:
    primary = chart_data.get("primary_chart")
    if not isinstance(primary, dict):
        # 舊版回應沒有 primary_chart，上圖資料在最外層
        primary = {
            "component_type": None,
            "points": chart_data.get("points") or [],
            "limits": chart_data.get("limits") or {},
        }
    else:
        # primary_chart.limits 可能沒有 USL/LSL，補上最外層的規格
        outer = chart_data.get("limits") if isinstance(chart_data.get("limits"), dict) else {}
        merged = dict(outer)
        merged.update({k: v for k, v in (primary.get("limits") or {}).items() if v is not None})
        primary = {**primary, "limits": merged}

    secondary = chart_data.get("secondary_chart")
    if not isinstance(secondary, dict):
        secondary = None

    selection = {}
    if isinstance(capability_data, dict) and isinstance(capability_data.get("selection"), dict):
        selection = capability_data["selection"]

    feature = (
        chart_data.get("feature_name")
        or selection.get("feature_name")
        or "未提供"
    )
    lines = [
        f"尺寸：{feature}",
        f"品號／製程：{chart_data.get('part_process') or '未提供'}",
        f"管制圖類型：{chart_data.get('chart_type') or selection.get('chart_type') or '未提供'}",
        "",
    ]

    primary_title = "上圖：" + COMPONENT_LABELS.get(
        str(primary.get("component_type")), "主圖"
    )
    primary_lines, primary_groups = _chart_section(primary_title, primary, with_spec=True)
    lines.extend(primary_lines)

    secondary_groups = None
    if secondary is not None:
        secondary_title = "下圖：" + COMPONENT_LABELS.get(
            str(secondary.get("component_type")), "副圖"
        )
        secondary_lines, secondary_groups = _chart_section(
            secondary_title, secondary, with_spec=False
        )
        lines.append("")
        lines.extend(secondary_lines)

    primary_limits = primary.get("limits") or {}
    phase_ii = _has_limits(primary_limits)
    chart_point_count = len([p for p in primary.get("points") or [] if isinstance(p, dict)])

    out_of_control = len(primary_groups["beyond_limits"]) + len(primary_groups["run_rule_only"])
    if secondary_groups is not None:
        out_of_control += len(secondary_groups["beyond_limits"]) + len(secondary_groups["run_rule_only"])

    # 整體判斷：把 Phase、失控、超規寫成一句，模型整句照抄就不會漏掉超規。
    spec_points = primary_groups["out_of_spec"]
    spec_clause = (
        f"有 {len(spec_points)} 點超規（{_compress_ids(spec_points)}，屬不良品）"
        if spec_points
        else "沒有超規點"
    )
    in_control: bool | None
    if not phase_ii:
        in_control = None
        verdict = (
            "系統處於 Phase I（尚未建立管制界線），只能判斷是否超規，"
            f"不可判斷製程是否穩定；目前{spec_clause}。"
        )
    elif out_of_control == 0:
        in_control = True
        verdict = f"系統處於 Phase II 監控中，製程目前沒有失控訊號；{spec_clause}。"
    else:
        in_control = False
        verdict = (
            f"系統處於 Phase II 監控中，但製程目前出現失控訊號，且{spec_clause}，"
            "需要調查特殊原因。"
        )

    lines.append("")
    lines.append("【整體判斷句】（【整體判斷】照抄這一句）")
    lines.append(verdict)

    # 異常重點清單：模型會在一堆重點裡挑著寫，所以列成清單要求逐條寫到。
    findings = [f"上圖：{hint}" for hint in _pattern_hints(primary_groups)]
    if secondary_groups is not None:
        findings += [
            f"下圖：{hint}"
            for hint in _secondary_hints(secondary.get("component_type"), secondary_groups)
        ]
    lines.append("")
    lines.append("【異常重點清單】（【異常重點】必須逐條寫到，一項都不能漏，點號照抄）")
    if findings:
        lines.extend(f"{index}. {item}" for index, item in enumerate(findings, start=1))
    else:
        lines.append("1. 上下圖都沒有異常點。")

    lines.append("")
    lines.extend(
        _capability_section(
            capability_data,
            chart_point_count=chart_point_count,
            in_control=in_control,
        )
    )

    recs = _recommendations(
        phase_ii=phase_ii,
        primary_groups=primary_groups,
        secondary_groups=secondary_groups,
        secondary_component=secondary.get("component_type") if secondary else None,
        capability_data=capability_data,
    )
    lines.append("")
    lines.append("【建議處置清單】（【建議處置】必須包含以下每一項，點號照抄）")
    lines.extend(f"{index}. {item}" for index, item in enumerate(recs, start=1))
    return "\n".join(lines)


SUMMARY_SYSTEM_PROMPT = (
    "你是台灣製造業工廠的品管工程師。"
    "一律使用繁體中文（台灣用語）回答，絕對不要使用簡體字。"
    "輸出純文字，不要使用 Markdown 符號（例如 **、#、`）。"
)


def build_summary_prompt(summary_context: str) -> str:
    return f"""請根據下方資料，為品管人員寫一份 SPC 管制圖摘要。

寫作規則：
1. 「系統判讀」是程式已經算好的結論，必須照用，不可推翻，也不可寫出相反或模稜兩可的說法。
2. 用語要分清楚：
   - 超規：點超出 USL/LSL。
   - 超出管制界限：點超出 UCL/LCL。
   - 違反連串規則：點仍在 UCL/LCL 內，但連續多點的排列異常；不可說成「超出管制界限」。
   - 失控：超出管制界限與違反連串規則的合稱。
   - 「Phase I／Phase II」是系統狀態（有沒有核准的管制界線）；「失控訊號」是製程狀態。
     兩者要分開講，例如「系統處於 Phase II 監控中，但製程目前出現失控訊號」，
     不要只寫「未處於統計管制狀態」，以免讀者誤以為系統沒有在監控。
3. 只能引用資料中出現的數字與點號，照抄，不可自行計算或補數字。
4. 不要捏造資料中沒有的機台、班別、人員、材料等資訊。
5. 【能力分析】用 2 到 4 句總結，不要把 Cp、Cpk、Cpm、Cpmk、Ppk 逐一各寫一句：
   - 第一句寫出 Cpk 數值、門檻數值，以及是否達到門檻（數字照抄系統判讀）。
   - 只有 Cpk 有門檻，不可說 Cp、Cpm、Cpmk、Ppk「達標」或「未達標」。
6. 【整體判斷】照抄「整體判斷句」。
7. 【異常重點】依「異常重點清單」逐條寫，清單上的每一項都要寫到，不可遺漏，點號照抄；
   可以分成上圖、下圖兩段。
8. 【建議處置】依「建議處置清單」逐條寫，清單上的每一項都要寫到，點號照抄；
   清單以外最多再補 1 點，且必須與資料一致。清單沒有寫時，不可建議調整或校正製程中心。
9. 引用資料時要寫成通順的句子，不要寫出「系統判讀」「清單」「寫作限制」等字樣；
   標有「（寫作限制，不要寫進摘要）」的內容只是給你的規則，不要出現在摘要裡。
10. 全文繁體中文、純文字，不要 Markdown 符號，不要重複相同的句子。

資料：
{summary_context}

請依照以下格式輸出，只輸出這四段：
【整體判斷】
（照抄整體判斷句）

【異常重點】
（逐條寫出異常重點清單的每一項）

【能力分析】
（2 到 4 句，第一句寫 Cpk 數值與門檻）

【建議處置】
（逐條寫出建議處置清單的每一項）
"""


async def generate_ai_summary(request: AiSummaryRequest) -> AiSummaryResponse:
    summary_context = build_summary_context(
        request.chart_data,
        request.capability_data,
    )
    prompt = build_summary_prompt(summary_context)
    summary = await generate_with_ollama(prompt, system=SUMMARY_SYSTEM_PROMPT)
    return AiSummaryResponse(summary=summary, summary_context=summary_context)
