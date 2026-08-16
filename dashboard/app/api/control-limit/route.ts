// GET /api/control-limit?product=...&process=...&machine=...&feature=...
//
// 診斷:列出 「管制圖」 表符合此定位的所有列(不管管制圖類型/啟用)。

import { NextRequest, NextResponse } from "next/server";
import { getSql } from "@/lib/neon";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const product = sp.get("product")?.trim();
  const process = sp.get("process")?.trim();
  const machine = sp.get("machine")?.trim();
  const feature = sp.get("feature")?.trim();

  if (!product || !process || !machine || !feature) {
    return NextResponse.json(
      {
        error: "缺少參數,需要 product / process / machine / feature",
      },
      { status: 400 },
    );
  }

  try {
    const sql = getSql();
    const rows = await sql`
      SELECT
        c."品號"                 AS product,
        c."製程"                 AS process,
        c."機台"                 AS machine,
        c."球標尺寸名"           AS feature_name,
        c."管制圖類型"           AS chart_type,
        c."管制中線一"::float8   AS cl,
        c."管制上界一"::float8   AS ucl,
        c."管制下界一"::float8   AS lcl,
        c."管制中線二"::float8   AS mr_cl,
        c."管制上界二"::float8   AS mr_ucl,
        c."管制下界二"::float8   AS mr_lcl,
        c."管制是否啟用"         AS is_active
      FROM "管制圖" c
      WHERE NORMALIZE(TRIM(c."品號"))       = NORMALIZE(TRIM(${product}))
        AND NORMALIZE(TRIM(c."製程"))       = NORMALIZE(TRIM(${process}))
        AND NORMALIZE(TRIM(c."機台"))       = NORMALIZE(TRIM(${machine}))
        AND NORMALIZE(TRIM(c."球標尺寸名")) = NORMALIZE(TRIM(${feature}))
    `;
    return NextResponse.json({
      product,
      process,
      machine,
      feature_name: feature,
      rows_found: (rows as unknown as unknown[]).length,
      rows,
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "連接資料庫來源錯誤", detail },
      { status: 500 },
    );
  }
}
