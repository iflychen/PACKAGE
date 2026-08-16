// GET /api/schema-check
// 專門看「管制圖」表當下的真實欄位。用新 URL 避免瀏覽器快取干擾。

import { NextResponse } from "next/server";
import { getSql } from "@/lib/neon";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const sql = getSql();
    const columns = await sql`
      SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name   = '管制圖'
       ORDER BY ordinal_position
    `;
    const now = new Date().toISOString();
    return NextResponse.json(
      { queried_at: now, table: "管制圖", columns },
      {
        headers: {
          "Cache-Control": "no-store, no-cache, must-revalidate",
        },
      },
    );
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "查 schema 失敗", detail },
      { status: 500 },
    );
  }
}
