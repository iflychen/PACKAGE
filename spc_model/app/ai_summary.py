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


def _format_limit_value(value: Any) -> str:
    if value is None:
        return "not provided"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _count_points(points: list[dict[str, Any]]) -> dict[str, int]:
    out_of_spec = 0
    out_of_control = 0
    normal = 0

    for point in points:
        if point.get("is_out_of_spec") is True:
            out_of_spec += 1
        elif point.get("is_out_of_control") is True:
            out_of_control += 1
        else:
            normal += 1

    return {
        "sample_count": len(points),
        "normal_count": normal,
        "out_of_spec_count": out_of_spec,
        "out_of_control_count": out_of_control,
    }


def _abnormal_points(points: list[dict[str, Any]], limit: int = 12) -> list[str]:
    rows: list[str] = []
    for point in points:
        if point.get("is_out_of_spec") is not True and point.get("is_out_of_control") is not True:
            continue
        rules = point.get("violated_rules") or []
        rule_text = ", ".join(str(rule) for rule in rules) if rules else "no rule code"
        rows.append(
            f"- x={point.get('x')}, value={_format_limit_value(point.get('value'))}, "
            f"out_of_spec={point.get('is_out_of_spec')}, "
            f"out_of_control={point.get('is_out_of_control')}, rules={rule_text}"
        )
        if len(rows) >= limit:
            break
    return rows


def _build_capability_context(capability_data: dict[str, Any] | None) -> list[str]:
    if not isinstance(capability_data, dict):
        return ["capability: not provided"]

    selection = capability_data.get("selection")
    if not isinstance(selection, dict):
        selection = {}
    metrics = capability_data.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    table_summary = capability_data.get("table_summary")
    if not isinstance(table_summary, dict):
        table_summary = {}
    daily = capability_data.get("daily")
    if not isinstance(daily, dict):
        daily = {}

    below_features = table_summary.get("below_threshold_features")
    if not isinstance(below_features, list):
        below_features = []
    below_rows: list[str] = []
    for item in below_features:
        if not isinstance(item, dict):
            continue
        below_rows.append(
            f"{item.get('feature_name')} (Cpk={_format_limit_value(item.get('cpk'))}, "
            f"n={_format_limit_value(item.get('sample_size'))})"
        )

    return [
        "capability:",
        f"- selected_feature: {selection.get('feature_name') or metrics.get('feature_name') or 'not provided'}",
        f"- sample_size: {_format_limit_value(metrics.get('sample_size'))}",
        f"- mean: {_format_limit_value(metrics.get('mean'))}",
        f"- sigma: {_format_limit_value(metrics.get('sigma'))}",
        f"- nominal_value: {_format_limit_value(metrics.get('nominal_value'))}",
        f"- USL: {_format_limit_value(metrics.get('usl'))}",
        f"- LSL: {_format_limit_value(metrics.get('lsl'))}",
        f"- Cp: {_format_limit_value(metrics.get('cp'))}",
        f"- Cpk: {_format_limit_value(metrics.get('cpk'))}",
        f"- Cpm: {_format_limit_value(metrics.get('cpm'))}",
        f"- Cpmk: {_format_limit_value(metrics.get('cpmk'))}",
        f"- Ppk: {_format_limit_value(metrics.get('ppk'))}",
        f"- cpk_threshold: {_format_limit_value(capability_data.get('cpk_threshold'))}",
        f"- meets_cpk_threshold: {_format_limit_value(capability_data.get('meets_cpk_threshold'))}",
        f"- feature_count: {_format_limit_value(table_summary.get('feature_count'))}",
        f"- below_threshold_count: {_format_limit_value(table_summary.get('below_threshold_count'))}",
        f"- unavailable_count: {_format_limit_value(table_summary.get('unavailable_count'))}",
        f"- below_threshold_features: {', '.join(below_rows) if below_rows else 'none'}",
        f"- observed_day_count: {_format_limit_value(daily.get('day_count'))}",
        f"- last_measured_at: {_format_limit_value(daily.get('last_measured_at'))}",
    ]


def build_summary_context(
    chart_data: dict[str, Any],
    capability_data: dict[str, Any] | None = None,
) -> str:
    points = chart_data.get("points")
    if not isinstance(points, list):
        points = []

    limits = chart_data.get("limits")
    if not isinstance(limits, dict):
        limits = {}

    counts = _count_points([p for p in points if isinstance(p, dict)])
    abnormal_rows = _abnormal_points([p for p in points if isinstance(p, dict)])

    primary_chart = chart_data.get("primary_chart")
    secondary_chart = chart_data.get("secondary_chart")
    primary_component = (
        primary_chart.get("component_type")
        if isinstance(primary_chart, dict)
        else None
    )
    secondary_component = (
        secondary_chart.get("component_type")
        if isinstance(secondary_chart, dict)
        else None
    )

    context_lines = [
        f"part_process: {chart_data.get('part_process') or 'not provided'}",
        f"feature_name: {chart_data.get('feature_name') or 'not provided'}",
        f"chart_type: {chart_data.get('chart_type') or 'not provided'}",
        f"primary_component: {primary_component or 'not provided'}",
        f"secondary_component: {secondary_component or 'not provided'}",
        f"sample_count: {counts['sample_count']}",
        f"normal_count: {counts['normal_count']}",
        f"out_of_spec_count: {counts['out_of_spec_count']}",
        f"out_of_control_count: {counts['out_of_control_count']}",
        "limits:",
        f"- USL: {_format_limit_value(limits.get('usl'))}",
        f"- UCL: {_format_limit_value(limits.get('ucl'))}",
        f"- CL: {_format_limit_value(limits.get('cl'))}",
        f"- LCL: {_format_limit_value(limits.get('lcl'))}",
        f"- LSL: {_format_limit_value(limits.get('lsl'))}",
        "abnormal_points:",
    ]
    context_lines.extend(abnormal_rows or ["- none"])
    context_lines.extend(_build_capability_context(capability_data))
    return "\n".join(context_lines)


def build_summary_prompt(summary_context: str) -> str:
    return f"""內容根據以下 SPC 管制圖資料產生摘要。

1. 整體製程狀態
2. 是否有超出規格或管制界限
3. 製程能力分析：解讀 Cp、Cpk、Cpm、Cpmk、Ppk 與 Cpk 門檻
4. 可能原因
5. 建議處置

- 只能根據提供的資料回答
- 不要捏造沒有提供的機台、班別、人員資訊
- 如果資料不足，請明確說資料不足
- 能力指標為 null / not provided 時，不可自行推測
- Cpk 低於門檻時要明確指出；Cp 高但 Cpk 低時，說明製程可能偏心
- Cpm / Cpmk 需連同目標值偏移解讀，Ppk 用於描述整體長期表現
- 不可只憑能力指標宣稱製程穩定，穩定性仍以管制圖訊號判斷

資料如下：
{summary_context}

固定格式:
【整體判斷】
...

【異常重點】
...

【能力分析】
...

【建議處置】
...
"""


async def generate_ai_summary(request: AiSummaryRequest) -> AiSummaryResponse:
    summary_context = build_summary_context(
        request.chart_data,
        request.capability_data,
    )
    prompt = build_summary_prompt(summary_context)
    summary = await generate_with_ollama(prompt)
    return AiSummaryResponse(summary=summary, summary_context=summary_context)
