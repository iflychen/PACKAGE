// GET /api/chart?product=...&process=...&machine=...&feature=...&chart_type=I-MR
//
// 新 schema:定位一組管制對象需要 品號 + 製程 + 機台 + 球標尺寸名 (+ 管制圖類型)。
// 回傳畫圖用的 I chart + MR chart 資料。

import { NextRequest, NextResponse } from "next/server";
import { getFeature } from "@/lib/db";
import {
  buildChartData,
  buildMrChart,
  chartSeriesToMrChartData,
  getSpcApiBase,
} from "@/lib/spcClient";
import type { ChartType } from "@/lib/types";
import { DEFAULT_EVENT_TYPE } from "@/lib/types";

export const dynamic = "force-dynamic";

const VALID_CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const product = sp.get("product")?.trim();
  const process = sp.get("process")?.trim();
  const machine = sp.get("machine")?.trim();
  const feature = sp.get("feature")?.trim();
  const chartTypeRaw = sp.get("chart_type")?.trim() ?? "I-MR";
  // 事件區間:有帶就只取該區間內的樣本(每個區間一張獨立管制圖)
  const intervalRaw = sp.get("event_interval_id")?.trim();
  const eventIntervalId =
    intervalRaw && Number.isFinite(Number(intervalRaw))
      ? Number(intervalRaw)
      : null;
  const eventType = sp.get("event_type")?.trim() || DEFAULT_EVENT_TYPE;

  if (!product || !process || !machine || !feature) {
    return NextResponse.json(
      {
        error: "缺少參數,需要 product / process / machine / feature",
      },
      { status: 400 },
    );
  }
  const chartType: ChartType = VALID_CHART_TYPES.includes(
    chartTypeRaw as ChartType,
  )
    ? (chartTypeRaw as ChartType)
    : "I-MR";

  // 1) DB
  let rec;
  try {
    rec = await getFeature(
      product,
      process,
      machine,
      feature,
      chartType,
      eventIntervalId,
      eventType,
    );
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "連接資料庫來源錯誤", detail },
      { status: 500 },
    );
  }
  if (!rec) {
    return NextResponse.json(
      {
        error: `找不到球標尺寸「${feature}」(品號=${product}, 製程=${process})`,
      },
      { status: 404 },
    );
  }

  // 1.5) 這個範圍內沒有任何量測值就直接回,不要把空陣列丟給 Python
  //      (Python 會回 422「measurements are required」,錯誤訊息很難懂)
  if (rec.measurements.length === 0) {
    return NextResponse.json(
      {
        error:
          eventIntervalId != null
            ? `這個「${eventType}」區間內沒有「${feature}」的量測值,請改選其他區間。`
            : `「${feature}」目前沒有任何量測值。`,
        empty: true,
        event_interval_id: eventIntervalId,
        event_type: eventType,
      },
      { status: 404 },
    );
  }

  // 2) Python
  try {
    const built = await buildChartData(
      `${product}::${process}::${machine}`,
      rec,
    );
    const activeLimit = rec.control_limit;
    // DB 是 active 狀態與正式界線的唯一權威。即使 Python 版本較舊、沒有把
    // request.control_limit 原樣帶回，也必須由 API response 補回正式界線，
    // 避免「DB 顯示 active、圖上卻沒有 UCL/CL/LCL」。
    const individuals = activeLimit
      ? {
          ...built,
          limits: {
            ...built.limits,
            cl: activeLimit.primary_cl ?? activeLimit.cl,
            ucl: activeLimit.primary_ucl ?? activeLimit.ucl,
            lcl: activeLimit.primary_lcl ?? activeLimit.lcl,
          },
          primary_chart: built.primary_chart
            ? {
                ...built.primary_chart,
                limits: {
                  ...built.primary_chart.limits,
                  cl: activeLimit.primary_cl ?? activeLimit.cl,
                  ucl: activeLimit.primary_ucl ?? activeLimit.ucl,
                  lcl: activeLimit.primary_lcl ?? activeLimit.lcl,
                },
              }
            : built.primary_chart,
          secondary_chart: built.secondary_chart
            ? {
                ...built.secondary_chart,
                limits: {
                  ...built.secondary_chart.limits,
                  cl: activeLimit.secondary_cl ?? null,
                  ucl: activeLimit.secondary_ucl ?? null,
                  lcl: activeLimit.secondary_lcl ?? null,
                },
              }
            : built.secondary_chart,
        }
      : built;
    const mr =
      chartSeriesToMrChartData(individuals.secondary_chart) ??
      (await buildMrChart(rec));
    return NextResponse.json({
      ...individuals,
      mr,
      product,
      process,
      machine,
      event_interval_id: eventIntervalId,
      event_type: eventType,
      has_active_control_limit: rec.has_active_control_limit === true,
      active_control_limit: activeLimit,
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    // 分辨「連不上」與「連上了但被 Python 拒絕」——後者常是資料問題,
    // 一律顯示「請確認 uvicorn 有在執行」只會讓人往錯的方向查。
    const reachedPython = detail.includes("SPC service 回傳");
    return NextResponse.json(
      {
        error: reachedPython
          ? "Python SPC 服務拒絕了這次請求(服務本身是通的)。"
          : "無法連線 Python SPC 服務,請確認 uvicorn 有在執行。",
        spc_api_base: getSpcApiBase(),
        detail,
      },
      { status: 502 },
    );
  }
}
