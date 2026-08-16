// POST /api/measurements/notify
//
// 外部系統寫完 Neon 後通知我們 (新 schema)。
//
// Body:
//   { product, process, machine, serial_no, feature_name, chart_type? }
// 我方讀 Neon 該筆 → 分析 → 更新 是否異常 / 異常類型 → 必要時建/修管制線。

import { NextRequest, NextResponse } from "next/server";
import { processMeasurement } from "@/lib/measurementProcessor";
import { getSpcApiBase } from "@/lib/spcClient";
import type { ChartType } from "@/lib/types";

export const dynamic = "force-dynamic";

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 200;
const VALID_CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

interface NotifyBody {
  product?: string;
  process?: string;
  machine?: string;
  serial_no?: number;
  feature_name?: string;
  chart_type?: string;
}

export async function POST(req: NextRequest) {
  let body: NotifyBody;
  try {
    body = (await req.json()) as NotifyBody;
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
    !featureName
  ) {
    return NextResponse.json(
      {
        error:
          "缺少必要欄位。需要 product, process, machine, serial_no, feature_name。",
      },
      { status: 400 },
    );
  }

  let lastErrMsg = "";
  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
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
      lastErrMsg = msg;

      const isRaceError = msg.includes("讀不到量測值");
      if (!isRaceError) {
        if (msg.startsWith("找不到球標尺寸")) {
          return NextResponse.json({ error: msg }, { status: 404 });
        }
        if (msg.includes("SPC service 回傳") || msg.includes("SPC")) {
          return NextResponse.json(
            {
              error: "Python SPC 服務錯誤",
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

      if (attempt < MAX_RETRIES) {
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
      }
    }
  }

  return NextResponse.json(
    {
      error: "讀不到指定的量測值,可能外部 INSERT 尚未 commit 或參數不對",
      detail: lastErrMsg,
      retried: MAX_RETRIES,
    },
    { status: 409 },
  );
}
