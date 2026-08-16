// GET /api/processes
// 回傳「有測量值」的所有 (品號, 製程, 機台, 球標尺寸名) 扁平清單,
// 前端會用它衍生 4 級級聯下拉選項。

import { NextResponse } from "next/server";
import { listFeatureCombos } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const combos = await listFeatureCombos();
    return NextResponse.json({ combos });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "連接資料庫來源錯誤", detail },
      { status: 500 },
    );
  }
}
