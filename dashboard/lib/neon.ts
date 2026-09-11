// ============================================================================
//  PostgreSQL client (node-postgres)
//  ---------------------------------------------------------------------------
//  這個檔案原本用 @neondatabase/serverless 的 neon()，那是 Neon 專屬的
//  SQL-over-HTTP driver，只能連 Neon 的雲端端點，連不上一般的 PostgreSQL
//  (例如 docker compose 裡的 postgres 服務)。容器化之後改用 node-postgres。
//
//  ⚠️ 檔名與匯出名稱刻意保留 (lib/neon.ts / hasNeon / getSql)：
//     lib/db.ts、app/api/db-info、app/api/neon-diagnose 等處都引用這些名稱，
//     改名等於要動好幾個檔案。這裡只換底層實作，對外介面完全不變。
//
//  ⚠️ 合併上游時請保留這個檔案的本版本。上游 (englishorspanish-2/RRealproject)
//     仍是 @neondatabase/serverless，拿上游那份去建 image 會連不上容器 postgres。
//
//  用法不變：
//    const rows = await sql`SELECT * FROM "球標尺寸" WHERE "品號" = ${p}`;
//  參數一樣會被轉成 $1, $2 帶入，不會有 SQL injection。
//
//  沒設 DATABASE_URL 時 hasNeon() 回 false；getSql() 會直接丟例外。
// ============================================================================

import { Pool, types } from "pg";

const DATABASE_URL = process.env.DATABASE_URL;

// ---------------------------------------------------------------------------
//  numeric → number
//  ---------------------------------------------------------------------------
//  node-postgres 預設把 numeric(OID 1700) 回傳成「字串」以免大數精度遺失。
//  本專案的量測值、公差、管制界線都是 numeric，而 lib/db.ts 幾乎都有
//  ::float8 轉型或用 Number() 包住，所以其實兩種都能運作；這裡統一轉成
//  number，是為了讓沒轉型的新查詢也不會意外拿到字串。
//
//  注意：如果日後有欄位真的會超過 2^53，要把這行拿掉並改在呼叫端處理。
// ---------------------------------------------------------------------------
types.setTypeParser(1700, (value) => (value === null ? null : Number(value)));

/** DATABASE_URL 需不需要走 TLS。Neon / 大部分雲端 PG 要，本機容器不要。 */
function sslOption(url: string): { rejectUnauthorized: boolean } | undefined {
  const lower = url.toLowerCase();
  const wantsSsl =
    lower.includes("sslmode=require") ||
    lower.includes("sslmode=verify") ||
    lower.includes("neon.tech");
  // 自簽憑證(本機/內網 PG)也要能連，所以不驗證憑證鏈。
  return wantsSsl ? { rejectUnauthorized: false } : undefined;
}

// ---------------------------------------------------------------------------
//  Pool 要快取在 globalThis
//  ---------------------------------------------------------------------------
//  Next.js dev 模式的 hot reload 會重複執行模組。每次都 new Pool() 的話，
//  連線數會一路往上疊到 PostgreSQL 的 max_connections 被打爆
//  (錯誤訊息長這樣：sorry, too many clients already)。
//
//  原本的 neon() 是 stateless HTTP，沒有這個問題；換成 Pool 之後就有了，
//  所以一定要掛在 globalThis 上。
// ---------------------------------------------------------------------------
declare global {
  // eslint-disable-next-line no-var
  var __spcPgPool: Pool | undefined;
}

function getPool(): Pool {
  if (!DATABASE_URL) {
    throw new Error(
      "DATABASE_URL 未設定；本機請填 spc-dashboard/.env.local，容器請用環境變數傳入。",
    );
  }
  if (!globalThis.__spcPgPool) {
    globalThis.__spcPgPool = new Pool({
      connectionString: DATABASE_URL,
      max: Number(process.env.PGPOOL_MAX ?? 10),
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 10_000,
      ssl: sslOption(DATABASE_URL),
    });
    // 沒有這個 handler 的話，閒置連線被 PG 端切斷會變成 unhandled error
    // 直接讓整個 Node process 掛掉。
    globalThis.__spcPgPool.on("error", (err) => {
      console.error("[pg pool] idle client error:", err.message);
    });
  }
  return globalThis.__spcPgPool;
}

export function hasNeon(): boolean {
  return Boolean(DATABASE_URL);
}

/** 和舊 NeonQueryFunction 相容的最小介面：回傳 row 陣列。 */
export type SqlTag = <T = Record<string, unknown>>(
  strings: TemplateStringsArray,
  ...values: unknown[]
) => Promise<T[]>;

/**
 * 拿 sql tag。沒設 DATABASE_URL 時丟例外(呼叫端要先用 hasNeon() 判斷)。
 *
 * 把 sql`... ${a} ... ${b}` 轉成 ("... $1 ... $2", [a, b])。
 *
 * ⚠️ 每個 ${} 一律配一個新的 $n，即使值完全相同也不共用。
 *
 * 曾經試過「相同的值共用同一個 $n」，那是錯的：PostgreSQL 是從「參數被用在
 * 哪個欄位」去推參數型別，同一個 $n 出現在兩個型別不同的欄位就會直接報
 * inconsistent types deduced for parameter $n。最典型的例子是兩個都傳 null
 * 的欄位，一個是 text、一個是 varchar，合併之後就炸了。
 *
 * 相對地，「同一個運算式要在 SELECT 和 GROUP BY 都出現」那種需求不該靠去重
 * 解決，而是把運算式收進子查詢、外層用欄位別名分組
 * (見 lib/db.ts 的 getDailySummary)。
 */
export function getSql(): SqlTag {
  const pool = getPool();

  return (async <T = Record<string, unknown>>(
    strings: TemplateStringsArray,
    ...values: unknown[]
  ): Promise<T[]> => {
    // undefined 不是合法的 pg 參數，一律當成 NULL。
    const params = values.map((value) => (value === undefined ? null : value));

    let text = "";
    for (let i = 0; i < strings.length; i += 1) {
      text += strings[i];
      if (i < values.length) text += `$${i + 1}`;
    }

    const result = await pool.query(text, params);
    return result.rows as T[];
  }) as SqlTag;
}
