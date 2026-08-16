// GET /api/db-info
//
// Neon 資料庫內省(introspection):
//   - 確認 DATABASE_URL 有讀到
//   - 列出 public schema 下所有表格
//   - 列出每張表的欄位名與型別
//
// 用途:當 lib/db.ts 的 SQL 因表名/欄名不符而 fallback 到種子資料時,
// 開這支確認實際 schema,再回去把 SQL 對齊即可。
//
// 開啟方式:啟動 next dev 後在瀏覽器打開
//   http://localhost:3000/api/db-info

import { NextResponse } from "next/server";
import { hasNeon, getSql } from "@/lib/neon";

export const dynamic = "force-dynamic";

export async function GET() {
  if (!hasNeon()) {
    return NextResponse.json(
      {
        connected: false,
        message:
          "DATABASE_URL 未設定。請在 spc-dashboard/.env.local 填入 Neon 連線字串後重啟 next dev。",
      },
      { status: 200 },
    );
  }

  try {
    const sql = getSql();

    // 1) 連線測試 + 資料庫基本資訊
    const meta = (await sql`
      SELECT current_database() AS database,
             current_user       AS user,
             version()          AS version
    `) as unknown as Array<{ database: string; user: string; version: string }>;

    // 2) 所有 user schema (排除 pg_catalog / information_schema)
    const tables = (await sql`
      SELECT table_schema, table_name
        FROM information_schema.tables
       WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
         AND table_type = 'BASE TABLE'
       ORDER BY table_schema, table_name
    `) as unknown as Array<{ table_schema: string; table_name: string }>;

    // 3) 各表欄位
    const columns = (await sql`
      SELECT table_schema, table_name, column_name, data_type,
             is_nullable, column_default
        FROM information_schema.columns
       WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
       ORDER BY table_schema, table_name, ordinal_position
    `) as unknown as Array<{
      table_schema: string;
      table_name: string;
      column_name: string;
      data_type: string;
      is_nullable: string;
      column_default: string | null;
    }>;

    // 依表分組
    const byTable = new Map<string, typeof columns>();
    for (const c of columns) {
      const key = `${c.table_schema}.${c.table_name}`;
      if (!byTable.has(key)) byTable.set(key, [] as typeof columns);
      byTable.get(key)!.push(c);
    }

    return NextResponse.json({
      connected: true,
      meta: meta[0],
      tables,
      columns_by_table: Object.fromEntries(byTable),
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { connected: false, error: "Neon 連線或查詢失敗", detail },
      { status: 500 },
    );
  }
}
