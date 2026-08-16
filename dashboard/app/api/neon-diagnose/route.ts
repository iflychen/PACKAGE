// GET /api/neon-diagnose
//
// 硬性診斷:告訴你 Neon 到底連到哪裡、對方有什麼。
// 用來排查「Neon Dashboard 看到新 schema,但 /api/db-info 顯示舊 schema」這種怪事。
//
// 回傳:
//   - 連線 metadata(資料庫名、使用者、伺服器 IP/port、PostgreSQL 版本、connection endpoint)
//   - 完整 public schema 表清單 + 建表時間
//   - 每張表最新一次 DDL 變動時間(能用時)
//   - 環境變數 DATABASE_URL 的 host + database(遮住密碼)
//
// 加了強制 no-cache header,瀏覽器一定拉新的。

import { NextResponse } from "next/server";
import { getSql } from "@/lib/neon";

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const fetchCache = "force-no-store";

export async function GET() {
  const startedAt = new Date().toISOString();

  // 遮密碼的 DATABASE_URL
  let sanitizedUrl = "(未設定)";
  const raw = process.env.DATABASE_URL;
  if (raw) {
    try {
      const u = new URL(raw);
      sanitizedUrl = `${u.protocol}//${u.username}:***@${u.hostname}${u.pathname}`;
    } catch {
      sanitizedUrl = "(無法解析)";
    }
  }

  try {
    const sql = getSql();

    // 1) 連線 metadata
    const meta = (await sql`
      SELECT
        current_database()                       AS database,
        current_user                             AS user_name,
        current_setting('server_version')        AS server_version,
        inet_server_addr()::text                 AS server_addr,
        inet_server_port()                       AS server_port,
        current_setting('cluster_name', true)    AS cluster_name,
        pg_backend_pid()                         AS backend_pid,
        now()                                    AS server_now
    `) as unknown as Array<Record<string, unknown>>;

    // 2) 所有 public schema 表 + 大約的建立時間(從 pg_class 拿 relfilenode 的 oid,可推順序)
    const tables = (await sql`
      SELECT
        c.relname                                             AS table_name,
        c.oid::text                                           AS oid,
        pg_size_pretty(pg_relation_size(c.oid))               AS size,
        (SELECT COUNT(*) FROM information_schema.columns
          WHERE table_schema='public'
            AND table_name = c.relname)                       AS column_count,
        (SELECT n_live_tup FROM pg_stat_user_tables
          WHERE relname = c.relname
            AND schemaname='public')                          AS row_count
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public'
        AND c.relkind = 'r'
      ORDER BY c.oid
    `) as unknown as Array<Record<string, unknown>>;

    // 3) 每張表的欄位
    const columns = (await sql`
      SELECT table_name, column_name, data_type, is_nullable, ordinal_position
        FROM information_schema.columns
       WHERE table_schema = 'public'
       ORDER BY table_name, ordinal_position
    `) as unknown as Array<{
      table_name: string;
      column_name: string;
      data_type: string;
      is_nullable: string;
      ordinal_position: number;
    }>;

    const columnsByTable: Record<string, unknown[]> = {};
    for (const c of columns) {
      if (!columnsByTable[c.table_name]) columnsByTable[c.table_name] = [];
      columnsByTable[c.table_name].push({
        column_name: c.column_name,
        data_type: c.data_type,
        is_nullable: c.is_nullable,
      });
    }

    // 4) branches / 資料庫層級的識別線索(Neon 沒官方 SQL 拿 branch id,但 hostname 有)
    const searchPath = (await sql`SHOW search_path`) as unknown as Array<{
      search_path: string;
    }>;

    return NextResponse.json(
      {
        queried_at: startedAt,
        database_url_sanitized: sanitizedUrl,
        connection: meta[0],
        search_path: searchPath[0]?.search_path,
        tables_summary: tables,
        columns_by_table: columnsByTable,
      },
      {
        headers: {
          "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
          Pragma: "no-cache",
          Expires: "0",
        },
      },
    );
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      {
        queried_at: startedAt,
        database_url_sanitized: sanitizedUrl,
        error: "查 schema 失敗",
        detail,
      },
      { status: 500 },
    );
  }
}
