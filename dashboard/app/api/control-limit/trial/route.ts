import { NextRequest, NextResponse } from "next/server";

import {
  approveFeatureTrial,
  calculateFeatureTrial,
  ControlLimitWorkflowError,
  type ControlLimitSelection,
} from "@/lib/controlLimitWorkflow";
import { getAutoCreateControlLimit } from "@/lib/config";
import { getSpcApiBase } from "@/lib/spcClient";
import type { ChartType } from "@/lib/types";
import { DEFAULT_EVENT_TYPE } from "@/lib/types";

export const dynamic = "force-dynamic";

const CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

interface TrialRequestBody extends Partial<ControlLimitSelection> {
  auto_approve_if_clean?: boolean;
}

function parseSelection(body: TrialRequestBody): ControlLimitSelection {
  const value = body;
  const product = typeof value.product === "string" ? value.product.trim() : "";
  const process = typeof value.process === "string" ? value.process.trim() : "";
  const machine = typeof value.machine === "string" ? value.machine.trim() : "";
  const featureName =
    typeof value.feature_name === "string" ? value.feature_name.trim() : "";
  const chartType = value.chart_type;
  if (
    !product ||
    !process ||
    !machine ||
    !featureName ||
    !chartType ||
    !CHART_TYPES.includes(chartType)
  ) {
    throw new ControlLimitWorkflowError(
      "需要 product、process、machine、feature_name 與有效的 chart_type。",
      400,
    );
  }
  const excludedPointIds = Array.isArray(value.excluded_point_ids)
    ? value.excluded_point_ids.filter(
        (id): id is number | string =>
          typeof id === "number" || typeof id === "string",
      )
    : [];
  const rawInterval = (value as { event_interval_id?: unknown }).event_interval_id;
  const eventIntervalId =
    typeof rawInterval === "number" && Number.isFinite(rawInterval)
      ? rawInterval
      : null;
  // 事件類型決定 工件_含事件 要取哪一列;省略時退回預設「換刀」,
  // 維持通用化之前的行為。
  const rawEventType = (value as { event_type?: unknown }).event_type;
  const eventType =
    typeof rawEventType === "string" && rawEventType.trim() !== ""
      ? rawEventType.trim()
      : DEFAULT_EVENT_TYPE;

  return {
    product,
    process,
    machine,
    feature_name: featureName,
    chart_type: chartType,
    excluded_point_ids: excludedPointIds,
    event_interval_id: eventIntervalId,
    event_type: eventType,
  };
}

export async function POST(req: NextRequest) {
  try {
    const body = (await req.json()) as TrialRequestBody;
    const selection = parseSelection(body);
    const result = await calculateFeatureTrial(selection);
    const primaryComponent =
      result.chart.primary_chart?.component_type ??
      (selection.chart_type === "I-MR" ? "I" : "XBAR");
    const secondaryComponent =
      result.chart.secondary_chart?.component_type ??
      (selection.chart_type === "I-MR"
        ? "MR"
        : selection.chart_type === "Xbar-R"
          ? "R"
          : "S");
    const suspectedPoints = [
      ...result.chart.points
        .filter(
          (point) => point.is_out_of_spec || point.is_out_of_control === true,
        )
        .map((point) => ({
          point_id: point.x,
          chart: "primary" as const,
          component_type: primaryComponent,
          actual_value: point.value,
          violated_rules: point.violated_rules,
        })),
      ...(result.chart.secondary_chart?.points ?? [])
        .filter((point) => point.is_out_of_control === true)
        .map((point) => ({
          point_id: point.x,
          chart: "secondary" as const,
          component_type: secondaryComponent,
          actual_value: point.value,
          violated_rules: point.violated_rules,
        })),
    ];

    const shouldAutoApprove =
      body.auto_approve_if_clean === true &&
      getAutoCreateControlLimit() &&
      selection.excluded_point_ids.length === 0 &&
      suspectedPoints.length === 0;
    if (shouldAutoApprove) {
      await approveFeatureTrial(result);
    }

    return NextResponse.json({
      ok: true,
      auto_approved: shouldAutoApprove,
      message: shouldAutoApprove
        ? "試算未發現疑似異常點，已自動建立 active 管制圖版本並進入 Phase II。"
        : undefined,
      selection: result.selection,
      sample_count: result.sample_count,
      subgroup_count: result.subgroup_count,
      excluded_point_ids: result.excluded_point_ids,
      trial: result.trial,
      suspected_points: suspectedPoints,
      control_start_time: result.control_start_time,
      chart: result.chart,
    });
  } catch (err) {
    if (err instanceof ControlLimitWorkflowError) {
      return NextResponse.json({ error: err.message }, { status: err.status });
    }
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      {
        error: "Phase I 試算失敗。",
        detail,
        spc_api_base: getSpcApiBase(),
      },
      { status: 500 },
    );
  }
}
