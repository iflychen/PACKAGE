import { NextRequest, NextResponse } from "next/server";

import {
  approveFeatureTrial,
  calculateFeatureTrial,
  ControlLimitWorkflowError,
  type ControlLimitSelection,
} from "@/lib/controlLimitWorkflow";
import { getAutoCreateControlLimit } from "@/lib/config";
import { syncTrialReviews } from "@/lib/db";
import { getSpcApiBase } from "@/lib/spcClient";
import type { TrialReviewRecord, TrialSignal } from "@/lib/trialReview";
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

    // 同一個 point_id 可能同時在上圖與下圖觸發。覆核表的主鍵是 point_id,
    // 所以要先合併成一列,兩張圖的來源收進 sources 陣列。
    //
    // actual_value 取先遇到的那個 —— suspectedPoints 是上圖在前,所以只要
    // 該點在上圖有觸發就會是量測值;只在下圖觸發的點拿到的是 MR/R/S 的值,
    // 那正是它被判異常的依據,顯示上也合理。
    const signalMap = new Map<string, TrialSignal>();
    for (const point of suspectedPoints) {
      const id = String(point.point_id);
      const existing = signalMap.get(id);
      if (existing) {
        existing.violated_rules = Array.from(
          new Set([...existing.violated_rules, ...point.violated_rules]),
        );
        existing.sources.push({
          chart: point.chart,
          component_type: point.component_type,
        });
      } else {
        signalMap.set(id, {
          point_id: id,
          actual_value: point.actual_value,
          violated_rules: [...point.violated_rules],
          sources: [
            { chart: point.chart, component_type: point.component_type },
          ],
        });
      }
    }

    // 寫入覆核表。這裡刻意不讓失敗中斷試算 —— 若某個環境還沒跑過
    // db/migrations 的建表腳本,試算與核准都應該照常可用,只是沒有覆核功能。
    let reviews: TrialReviewRecord[] = [];
    let reviewError: string | undefined;
    try {
      reviews = await syncTrialReviews(
        {
          product: selection.product,
          process: selection.process,
          machine: selection.machine,
          feature_name: selection.feature_name,
          chart_type: selection.chart_type,
          event_interval_id: selection.event_interval_id ?? null,
        },
        Array.from(signalMap.values()),
      );
    } catch (err) {
      reviewError = err instanceof Error ? err.message : String(err);
      console.error("[trial] syncTrialReviews failed:", reviewError);
    }

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
      reviews,
      review_error: reviewError,
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
