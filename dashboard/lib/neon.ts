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
 * 相同的值會共用同一個 $n —— 這不只是省參數。PostgreSQL 判斷
 * 「SELECT 的運算式有沒有出現在 GROUP BY」是比對語法樹，$1 和 $3 就算值一樣
 * 也算不同運算式。lib/db.ts 的 getDailySummary 正好在 SELECT 和 GROUP BY
 * 都用了 date_trunc(${unit}, ...)，不去重的話會被 PG 判定為
 * 「column must appear in the GROUP BY clause」。
 */
export function getSql(): SqlTag {
  const pool = getPool();

  return (async <T = Record<string, unknown>>(
    strings: TemplateStringsArray,
    ...values: unknown[]
  ): Promise<T[]> => {
    const params: unknown[] = [];
    const seen = new Map<unknown, number>();

    let text = "";
    for (let i = 0; i < strings.length; i += 1) {
      text += strings[i];
      if (i < values.length) {
        // undefined 不是合法的 pg 參數，一律當成 NULL。
        const value = values[i] === undefined ? null : values[i];
        let index = seen.get(value);
        if (index === undefined) {
          params.push(value);
          index = params.length;
          seen.set(value, index);
        }
        text += `$${index}`;
      }
    }

    const result = await pool.query(text, params);
    return result.rows as T[];
  }) as SqlTag;
}
