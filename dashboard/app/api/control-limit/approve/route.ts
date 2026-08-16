import { NextRequest, NextResponse } from "next/server";

import {
  approveFeatureTrial,
  calculateFeatureTrial,
  ControlLimitWorkflowError,
  type ControlLimitSelection,
} from "@/lib/controlLimitWorkflow";
import { getSpcApiBase } from "@/lib/spcClient";
import type { ChartType } from "@/lib/types";
import { DEFAULT_EVENT_TYPE } from "@/lib/types";

export const dynamic = "force-dynamic";

const CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

function parseSelection(body: unknown): ControlLimitSelection {
  const value = body as Partial<ControlLimitSelection>;
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
    const selection = parseSelection(await req.json());
    // 核准時由伺服器重新試算，避免採用過期或被竄改的前端界線。
    const result = await calculateFeatureTrial(selection);
    await approveFeatureTrial(result);
    return NextResponse.json({
      ok: true,
      message: "管制線版本已核准並啟用，後續資料將進入 Phase II。",
      selection: result.selection,
      control_start_time: result.control_start_time,
      limits: {
        cl: result.trial.cl,
        ucl: result.trial.ucl,
        lcl: result.trial.lcl,
        secondary: result.trial.bottom_chart,
      },
    });
  } catch (err) {
    if (err instanceof ControlLimitWorkflowError) {
      return NextResponse.json({ error: err.message }, { status: err.status });
    }
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      {
        error: "核准管制線失敗。",
        detail,
        spc_api_base: getSpcApiBase(),
      },
      { status: 500 },
    );
  }
}
