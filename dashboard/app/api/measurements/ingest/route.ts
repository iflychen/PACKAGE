// POST /api/measurements/ingest
//
// 外部系統丟新量測值進來的入口 (新 schema)。
//
// Body:
//   {
//     product:      string   // 品號
//     process:      string   // 製程
//     machine:      string   // 機台
//     serial_no:    number   // 流水號
//     feature_name: string   // 球標尺寸名
//     actual_value: number   // 實際值
//     measured_at?: string   // ISO,可省略,預設 now()
//     measured_by?: string   // 量測人員,可省略
//     chart_type?:  string   // 管制圖類型,預設 I-MR
//   }

import { NextRequest, NextResponse } from "next/server";
import { getSql } from "@/lib/neon";
import { processMeasurement } from "@/lib/measurementProcessor";
import { getSpcApiBase } from "@/lib/spcClient";
import { getMinSamples } from "@/lib/config";
import type { ChartType } from "@/lib/types";

export const dynamic = "force-dynamic";

const VALID_CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

interface IngestBody {
  product?: string;
  process?: string;
  machine?: string;
  serial_no?: number;
  feature_name?: string;
  actual_value?: number;
  measured_at?: string;
  measured_by?: string;
  chart_type?: string;
}

export async function POST(req: NextRequest) {
  let body: IngestBody;
  try {
    body = (await req.json()) as IngestBody;
  } catch {
    return NextResponse.json(
      { error: "request body 不是合法 JSON" },
      { status: 400 },
    );
  }

  const product = body.product?.trim();
  const process = body.process?.trim();
  const machine = body.machine?.trim();
  const serialNo = body.serial_no;
  const featureName = body.feature_name?.trim();
  const actualValue = body.actual_value;
  const measuredAt = body.measured_at;
  const measuredBy = body.measured_by?.trim() || null;
  const chartTypeRaw = body.chart_type?.trim() ?? "I-MR";
  const chartType: ChartType = VALID_CHART_TYPES.includes(
    chartTypeRaw as ChartType,
  )
    ? (chartTypeRaw as ChartType)
    : "I-MR";

  if (
    !product ||
    !process ||
    !machine ||
    typeof serialNo !== "number" ||
    !Number.isInteger(serialNo) ||
    !featureName ||
    typeof actualValue !== "number" ||
    !Number.isFinite(actualValue) ||
    (measuredAt != null && !Number.isFinite(Date.parse(measuredAt)))
  ) {
    return NextResponse.json(
      {
        error:
          "缺少必要欄位或格式錯誤。需要 product, process, machine, serial_no, feature_name, actual_value(有限數字)，measured_at 必須是有效日期。",
        threshold_hint: `Phase I 門檻:${getMinSamples()} 筆`,
      },
      { status: 400 },
    );
  }

  // 1) 寫入 工件 + 測量值。
  //
  // DB 真正的 PK 是：
  //   工件   (機台, 流水號)
  //   測量值 (機台, 流水號, 球標尺寸名)
  // 品號／製程不在 PK 內，不能放進 ON CONFLICT target。兩筆寫入收在同一個
  // data-modifying CTE，尺寸 FK 等任一錯誤都會讓整句 rollback，不留下孤立工件。
  try {
    const sql = getSql();
    const measuredAtValue = measuredAt ?? new Date().toISOString();
    const rows = (await sql`
      WITH inserted_workpiece AS (
        INSERT INTO "工件"
          ("品號", "製程", "機台", "流水號", "量測時間", "量測人員")
        VALUES
          (${product}, ${process}, ${machine}, ${serialNo},
           ${measuredAtValue}, ${measuredBy})
        ON CONFLICT ("機台", "流水號") DO NOTHING
        RETURNING 1
      ),
      valid_workpiece AS (
        SELECT 1 FROM inserted_workpiece
        UNION ALL
        SELECT 1
          FROM "工件" w
         WHERE w."機台" = ${machine}
           AND w."流水號" = ${serialNo}
           AND NORMALIZE(TRIM(w."品號")) = NORMALIZE(TRIM(${product}))
           AND NORMALIZE(TRIM(w."製程")) = NORMALIZE(TRIM(${process}))
        LIMIT 1
      ),
      upserted_measurement AS (
        INSERT INTO "測量值"
          ("品號", "製程", "機台", "流水號", "球標尺寸名",
           "實際值", "是否異常", "異常類型")
        SELECT
          ${product}, ${process}, ${machine}, ${serialNo}, ${featureName},
          ${actualValue}, FALSE, NULL
        WHERE EXISTS (SELECT 1 FROM valid_workpiece)
        ON CONFLICT ("機台", "流水號", "球標尺寸名")
        DO UPDATE SET
          "實際值" = EXCLUDED."實際值",
          "是否異常" = FALSE,
          "異常類型" = NULL
        WHERE NORMALIZE(TRIM("測量值"."品號")) = NORMALIZE(TRIM(EXCLUDED."品號"))
          AND NORMALIZE(TRIM("測量值"."製程")) = NORMALIZE(TRIM(EXCLUDED."製程"))
        RETURNING 1
      )
      SELECT
        EXISTS (SELECT 1 FROM valid_workpiece) AS workpiece_ok,
        EXISTS (SELECT 1 FROM upserted_measurement) AS measurement_ok
    `) as unknown as Array<{
      workpiece_ok: boolean;
      measurement_ok: boolean;
    }>;

    if (!rows[0]?.workpiece_ok) {
      return NextResponse.json(
        {
          error: "流水號衝突",
          detail: `機台 ${machine} 的流水號 ${serialNo} 已屬於其他品號或製程。`,
        },
        { status: 409 },
      );
    }
    if (!rows[0]?.measurement_ok) {
      return NextResponse.json(
        {
          error: "量測值識別衝突",
          detail: `機台 ${machine}、流水號 ${serialNo}、尺寸 ${featureName} 已屬於其他品號或製程。`,
        },
        { status: 409 },
      );
    }
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "寫入資料庫失敗", detail },
      { status: 500 },
    );
  }

  // 2) 分析
  try {
    const result = await processMeasurement({
      product,
      process,
      machine,
      serial_no: serialNo,
      feature_name: featureName,
      chart_type: chartType,
    });
    return NextResponse.json(result);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.startsWith("找不到球標尺寸")) {
      return NextResponse.json({ error: msg }, { status: 404 });
    }
    if (msg.includes("SPC service 回傳") || msg.includes("SPC")) {
      return NextResponse.json(
        {
          error: "無法連線 Python SPC 服務。",
          detail: msg,
          spc_api_base: getSpcApiBase(),
        },
        { status: 502 },
      );
    }
    return NextResponse.json(
      { error: "處理量測失敗", detail: msg },
      { status: 500 },
    );
  }
}
