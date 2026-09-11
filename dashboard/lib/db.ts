// ============================================================================
//  DB 接點 — Neon schema(2026-07 以 information_schema 實測確認)
//  ---------------------------------------------------------------------------
//  BASE TABLE
//    品號      PK(品號)
//    製程      PK(品號, 製程)
//    機台      PK(機台)
//    球標尺寸  PK(品號, 製程, 球標尺寸名) + 上公差 / 下公差 / 定義值
//    工件      PK(機台, 流水號)           + 品號 / 製程, 量測時間, 量測人員
//    測量值    PK(機台, 流水號, 球標尺寸名) + 品號 / 製程, 實際值,
//                                            是否異常 boolean NOT NULL, 異常類型
//    事件紀錄  PK(id) + 機台, 起始流水號, 事件時間, 事件類型, 備註
//              (原「換刀紀錄」,已通用化;事件類型為自由文字:換刀/保養/…)
//    管制圖    PK(品號, 製程, 機台, 球標尺寸名, 管制圖類型, 管制開始時間)
//              + 管制結束時間, 六條界線, 管制是否啟用, cp/cpk/cpm/cpmk/ppk
//    來源檔案  PK(檔名) — 本前端未使用
//
//  VIEW
//    事件使用區間 (事件紀錄id, 機台, 事件類型, 起始流水號,
//                  結束流水號_不含, 事件時間)
//        事件紀錄 加上用 LEAD() 推出的區間上界(半開區間,NULL = 進行中)
//        ——「同一事件類型內」才連續,所以查詢一定要帶事件類型
//    工件_含事件 (工件全欄 + 事件類型, 事件紀錄id, 事件啟用時間)
//        工件 已對應好所屬事件區間 —— 量測值要按區間篩選時 JOIN 這張最省事
//
//  幾個容易踩到的點:
//  1. 工件 / 測量值 的 PK 不含 品號 / 製程 → JOIN 只能也只需用 (機台, 流水號)。
//     多帶品號 / 製程 去 JOIN 反而會因為兩表值不一致而漏資料。
//  2. 流水號 是「機台層級」唯一,跨品號 / 製程共用同一個序號空間,
//     所以事件區間(按機台 + 流水號範圍)本來就可能跨多個品號。
//  3. 管制開始時間 屬於 管制圖 PK 且 NOT NULL → 不可寫 NULL,
//     且同一時間點只能有一筆 → approve 必須用 ON CONFLICT upsert。
//  4. 是否異常 是 boolean NOT NULL → 直接用 = FALSE 判乾淨樣本即可。
//  5. ⚠️ 工件_含事件 對「一件工件 × 每一種事件類型」各出一列 —— 這不是資料
//     重複。JOIN 時若不在 ON 條件帶上事件類型,量測值會被乘以事件類型數量,
//     Cpk / 樣本數 / 管制開始時間 會靜靜地算錯而不報錯。
//     本檔所有 JOIN "工件_含事件" 都必須帶 事件類型,且要放在 ON 而非 WHERE
//     (LEFT JOIN 放 WHERE 會退化成 INNER JOIN,沒有事件紀錄的工件會消失)。
// ============================================================================

import type {
  FeatureRecord,
  FeatureCombo,
  Measurement,
  Spec,
  ControlLimit,
  ChartType,
} from "./types";
import { DEFAULT_EVENT_TYPE } from "./types";
import { getSql } from "./neon";
import type {
  BaselineDisposition,
  ExclusionReasonCode,
  TrialReviewKey,
  TrialReviewRecord,
} from "./trialReview";

/**
 * 列出「有測量值」的所有 (品號, 製程, 機台, 球標尺寸名) 組合,
 * 給前端 4 級級聯下拉衍生選項。
 */
export async function listFeatureCombos(): Promise<FeatureCombo[]> {
  const sql = getSql();
  const rows = (await sql`
    SELECT DISTINCT
      m."品號"        AS product,
      m."製程"        AS process,
      m."機台"        AS machine,
      m."球標尺寸名"  AS feature_name,
      COALESCE(
        (
          SELECT c."管制圖類型" FROM "管制圖" c
           WHERE c."品號"       = m."品號"
             AND c."製程"       = m."製程"
              AND c."機台"       = m."機台"
              AND c."球標尺寸名" = m."球標尺寸名"
              AND c."管制是否啟用" = TRUE
              AND c."管制開始時間" <= CURRENT_TIMESTAMP
              AND (c."管制結束時間" IS NULL OR c."管制結束時間" > CURRENT_TIMESTAMP)
            ORDER BY c."管制開始時間" DESC
           LIMIT 1
        ),
        'I-MR'
      )              AS chart_type,
      EXISTS (
        SELECT 1 FROM "管制圖" c
         WHERE c."品號"       = m."品號"
           AND c."製程"       = m."製程"
            AND c."機台"       = m."機台"
            AND c."球標尺寸名" = m."球標尺寸名"
            AND c."管制是否啟用" = TRUE
            AND c."管制開始時間" <= CURRENT_TIMESTAMP
            AND (c."管制結束時間" IS NULL OR c."管制結束時間" > CURRENT_TIMESTAMP)
      )              AS has_active_control_limit,
      COALESCE(
        (SELECT array_agg(DISTINCT TRIM(c."管制圖類型"))
           FROM "管制圖" c
          WHERE c."品號"       = m."品號"
            AND c."製程"       = m."製程"
            AND c."機台"       = m."機台"
            AND c."球標尺寸名" = m."球標尺寸名"
            AND c."管制是否啟用" = TRUE
            AND c."管制開始時間" <= CURRENT_TIMESTAMP
            AND (c."管制結束時間" IS NULL OR c."管制結束時間" > CURRENT_TIMESTAMP)),
        ARRAY[]::text[]
      )              AS active_chart_types,
      (SELECT COUNT(*)::int FROM "測量值" m2
        WHERE m2."品號"       = m."品號"
          AND m2."製程"       = m."製程"
          AND m2."機台"       = m."機台"
          AND m2."球標尺寸名" = m."球標尺寸名"
      )              AS sample_size,
      (SELECT m3."實際值"::float8 FROM "測量值" m3
        WHERE m3."品號"       = m."品號"
          AND m3."製程"       = m."製程"
          AND m3."機台"       = m."機台"
          AND m3."球標尺寸名" = m."球標尺寸名"
        ORDER BY m3."流水號" DESC
        LIMIT 1
      )              AS latest_value
    FROM "測量值" m
    ORDER BY product, process, machine, feature_name
  `) as unknown as Array<{
    product: string;
    process: string;
    machine: string;
    feature_name: string;
    chart_type: string;
    has_active_control_limit: boolean;
    active_chart_types: string[];
    sample_size: number;
    latest_value: number | null;
  }>;

  return rows.map((r) => ({
    product: r.product,
    process: r.process,
    machine: r.machine,
    feature_name: r.feature_name,
    chart_type: (r.chart_type as ChartType) ?? "I-MR",
    has_active_control_limit: Boolean(r.has_active_control_limit),
    active_chart_types: (r.active_chart_types ?? []).filter(
      (value): value is ChartType =>
        value === "I-MR" || value === "Xbar-R" || value === "Xbar-S",
    ),
    sample_size: Number(r.sample_size) || 0,
    latest_value: r.latest_value == null ? null : Number(r.latest_value),
  }));
}

/**
 * 拿某個 (品號, 製程, 機台, 球標尺寸名, 管制圖類型) 完整資料:
 * spec + active control_limit + 所有歷史量測值(依時間排序)。
 */
export async function getFeature(
  product: string,
  process: string,
  machine: string,
  featureName: string,
  chartType: ChartType = "I-MR",
  eventIntervalId: number | null = null,
  eventType: string = DEFAULT_EVENT_TYPE,
): Promise<FeatureRecord | undefined> {
  const sql = getSql();

  // 1) spec (球標尺寸)
  const specRows = (await sql`
    SELECT
      f."定義值"::float8 AS nominal_value,
      f."上公差"::float8 AS upper_tolerance,
      f."下公差"::float8 AS lower_tolerance
    FROM "球標尺寸" f
    WHERE NORMALIZE(TRIM(f."品號"))       = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(f."製程"))       = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(f."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
    LIMIT 1
  `) as unknown as Array<{
    nominal_value: number | null;
    upper_tolerance: number | null;
    lower_tolerance: number | null;
  }>;

  if (specRows.length === 0) return undefined;
  const sp = specRows[0];
  const spec: Spec = {
    nominal_value: Number(sp.nominal_value ?? 0),
    upper_tolerance: Number(sp.upper_tolerance ?? 0),
    lower_tolerance: Number(sp.lower_tolerance ?? 0),
  };

  // 2) active 管制圖
  //
  //    刻意拆成兩個查詢,因為「有沒有 active 版本」和「要畫哪一組界線」是兩個
  //    不同的問題,混在一起會顧此失彼:
  //
  //    2a) 完整 key(品號 + 製程 + 機台 + 尺寸 + 圖型),不帶區間。
  //        事件區間只決定顯示哪些量測點,不屬於 active key。若此完整 key 已有
  //        active 版本,切到沒有該尺寸資料的新區間也不能退回 Phase I 或再次試算。
  //
  //    2b) 再從中挑出「屬於所選區間」的那一版界線。管制圖 沒有事件紀錄id 欄位,
  //        只能靠 管制開始時間 落在該區間的量測時間範圍內來對應(管制開始時間
  //        定義為該區間第 N 筆乾淨樣本的量測時間,必然落在區間內)。
  //        少了這一步,選舊區間會畫出最新版本的界線 —— 點是舊區間的、線是新
  //        區間的,畫面看起來正常但完全對不上,而且不會有任何錯誤訊息。
  //
  //    所以「該區間還沒核准過界線」的狀態是:has_active = true(不跳試算按鈕)
  //    但 control_limit = null(圖照畫,只是沒有管制線)。
  //
  //    ⚠️ 管制圖 也沒有事件類型欄位 —— 目前只有「換刀」會建管制線。
  //    若日後要對其他事件類型也建線,兩種類型的區間可能涵蓋同一批量測值,
  //    算出的 管制開始時間 會相同而互相覆蓋同一筆版本;
  //    屆時 管制圖 必須加上事件類型欄位並納入 PK。
  const clRows = (await sql`
    SELECT
      c."管制圖類型"           AS chart_type,
      c."管制中線一"::float8   AS cl,
      c."管制上界一"::float8   AS ucl,
      c."管制下界一"::float8   AS lcl,
      c."管制中線二"::float8   AS secondary_cl,
      c."管制上界二"::float8   AS secondary_ucl,
      c."管制下界二"::float8   AS secondary_lcl,
      c."管制是否啟用"         AS is_active
    FROM "管制圖" c
    WHERE NORMALIZE(TRIM(c."品號"))       = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(c."製程"))       = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(c."機台"))       = NORMALIZE(TRIM(${machine}))
      AND NORMALIZE(TRIM(c."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
      AND NORMALIZE(TRIM(c."管制圖類型")) = NORMALIZE(TRIM(${chartType}))
      AND c."管制是否啟用" = TRUE
      AND c."管制開始時間" <= CURRENT_TIMESTAMP
      AND (c."管制結束時間" IS NULL OR c."管制結束時間" > CURRENT_TIMESTAMP)
    ORDER BY c."管制開始時間" DESC
    LIMIT 1
  `) as unknown as Array<{
    chart_type: string;
    cl: number | null;
    ucl: number | null;
    lcl: number | null;
    secondary_cl: number | null;
    secondary_ucl: number | null;
    secondary_lcl: number | null;
    is_active: boolean | string;
  }>;

  const pickActive = (rows: typeof clRows) =>
    rows.find(
      (r) =>
        r.is_active === true ||
        (r.is_active as unknown as string) === "t" ||
        (r.is_active as unknown as string) === "true",
    );

  // 2a) 完整 key 有沒有 active 版本 —— 只用來決定要不要顯示 Phase I 試算按鈕。
  const activeRow = pickActive(clRows);

  // 2b) 所選區間自己的那一版界線。
  //     不分區間模式(eventIntervalId 為 null)就沿用 2a 的結果,不必多查一次。
  const intervalRows: typeof clRows =
    eventIntervalId == null
      ? []
      : ((await sql`
    WITH span AS (
      SELECT MIN(w."量測時間") AS t0, MAX(w."量測時間") AS t1
      FROM "測量值" m
      JOIN "工件_含事件" w
        ON w."機台"   = m."機台"
       AND w."流水號" = m."流水號"
       AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
      WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
        AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
        AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
        AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
        AND w."事件紀錄id" = ${eventIntervalId}::int
    )
    SELECT
      c."管制圖類型"           AS chart_type,
      c."管制中線一"::float8   AS cl,
      c."管制上界一"::float8   AS ucl,
      c."管制下界一"::float8   AS lcl,
      c."管制中線二"::float8   AS secondary_cl,
      c."管制上界二"::float8   AS secondary_ucl,
      c."管制下界二"::float8   AS secondary_lcl,
      c."管制是否啟用"         AS is_active
    FROM "管制圖" c
    WHERE NORMALIZE(TRIM(c."品號"))       = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(c."製程"))       = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(c."機台"))       = NORMALIZE(TRIM(${machine}))
      AND NORMALIZE(TRIM(c."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
      AND NORMALIZE(TRIM(c."管制圖類型")) = NORMALIZE(TRIM(${chartType}))
      AND c."管制是否啟用" = TRUE
      AND c."管制開始時間" <= CURRENT_TIMESTAMP
      AND (c."管制結束時間" IS NULL OR c."管制結束時間" > CURRENT_TIMESTAMP)
      AND c."管制開始時間" BETWEEN
            (SELECT t0 FROM span) AND (SELECT t1 FROM span)
    ORDER BY c."管制開始時間" DESC
    LIMIT 1
  `) as unknown as typeof clRows);

  const limitRow =
    eventIntervalId == null ? activeRow : pickActive(intervalRows);

  const control_limit: ControlLimit | null =
    limitRow && limitRow.cl != null && limitRow.ucl != null && limitRow.lcl != null
      ? {
          // SQL 比對已做 TRIM/NORMALIZE；回傳也要收斂成 request 的 canonical 值，
          // 否則 DB 若含尾端空白，spcClient 會誤判 chart type 不同而丟掉界線。
          chart_type: chartType,
          cl: Number(limitRow.cl),
          ucl: Number(limitRow.ucl),
          lcl: Number(limitRow.lcl),
          primary_cl: Number(limitRow.cl),
          primary_ucl: Number(limitRow.ucl),
          primary_lcl: Number(limitRow.lcl),
          secondary_cl:
            limitRow.secondary_cl == null ? null : Number(limitRow.secondary_cl),
          secondary_ucl:
            limitRow.secondary_ucl == null ? null : Number(limitRow.secondary_ucl),
          secondary_lcl:
            limitRow.secondary_lcl == null ? null : Number(limitRow.secondary_lcl),
          is_active: true,
        }
      : null;

  // 3) 歷史量測值,依 工件.量測時間 排序
  //    eventIntervalId 有值時只取該事件區間內的樣本。
  const mRows = (await sql`
    SELECT
      m."流水號"           AS measurement_id,
      m."流水號"           AS serial_no,
      to_char(w."量測時間", 'YYYY-MM-DD"T"HH24:MI:SS') AS measured_at,
      m."實際值"::float8   AS actual_value,
      m."是否異常"         AS is_abnormal
    FROM "測量值" m
    LEFT JOIN "工件_含事件" w
      ON w."機台"   = m."機台"
     AND w."流水號" = m."流水號"
     AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
    WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
      AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
      AND (${eventIntervalId}::int IS NULL
           OR w."事件紀錄id" = ${eventIntervalId}::int)
    ORDER BY w."量測時間" NULLS LAST, m."流水號"
  `) as unknown as Array<{
    measurement_id: number;
    serial_no: number;
    measured_at: string | null;
    actual_value: number;
    is_abnormal: boolean;
  }>;

  const measurements: Measurement[] = mRows.map((r) => ({
    measurement_id: Number(r.measurement_id),
    serial_no: Number(r.serial_no),
    // 已在 SQL 端 to_char 成字串。不可再經過 new Date():
    // 量測時間 是 timestamp(無時區),轉成 Date 會被當本地時間再轉 UTC,
    // 整批偏移時區差(UTC+8 就少 8 小時),寫回 DB 時就對不上原始值。
    measured_at: r.measured_at ?? undefined,
    actual_value: Number(r.actual_value),
    is_abnormal: r.is_abnormal === true,
  }));

  return {
    product,
    process,
    machine,
    feature_name: featureName,
    chart_type: chartType,
    feature_id: `${product}::${process}::${machine}::${featureName}`,
    spec,
    control_limit,
    has_active_control_limit: activeRow != null,
    measurements,
  };
}

// ============================================================================
//  儀表板用查詢
// ============================================================================

/** 某個 (品號, 製程, 機台) 底下所有球標尺寸的規格 + 量測值(給 CPK 統計表) */
export interface FeatureSpecWithValues {
  feature_name: string;
  spec: Spec;
  /** 乾淨樣本(是否異常 = FALSE)—— 製程能力一律用這組算 */
  values: number[];
  /** 乾淨樣本數 */
  sample_size: number;
  /** 含異常在內的總筆數,只用於顯示 */
  total_size: number;
}

export async function listFeatureSpecsWithValues(
  product: string,
  process: string,
  machine: string,
  eventIntervalId: number | null = null,
  eventType: string = DEFAULT_EVENT_TYPE,
): Promise<FeatureSpecWithValues[]> {
  const sql = getSql();

  const rows = (await sql`
    SELECT
      m."球標尺寸名"       AS feature_name,
      f."定義值"::float8   AS nominal_value,
      f."上公差"::float8   AS upper_tolerance,
      f."下公差"::float8   AS lower_tolerance,
      m."實際值"::float8   AS actual_value,
      m."是否異常"         AS is_abnormal
    FROM "測量值" m
    LEFT JOIN "球標尺寸" f
      ON NORMALIZE(TRIM(f."品號"))       = NORMALIZE(TRIM(m."品號"))
     AND NORMALIZE(TRIM(f."製程"))       = NORMALIZE(TRIM(m."製程"))
     AND NORMALIZE(TRIM(f."球標尺寸名")) = NORMALIZE(TRIM(m."球標尺寸名"))
    LEFT JOIN "工件_含事件" w
      ON w."機台"   = m."機台"
     AND w."流水號" = m."流水號"
     AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
    WHERE NORMALIZE(TRIM(m."品號")) = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(m."製程")) = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(m."機台")) = NORMALIZE(TRIM(${machine}))
      AND (${eventIntervalId}::int IS NULL
           OR w."事件紀錄id" = ${eventIntervalId}::int)
    ORDER BY m."球標尺寸名", w."量測時間" NULLS LAST, m."流水號"
  `) as unknown as Array<{
    feature_name: string;
    nominal_value: number | null;
    upper_tolerance: number | null;
    lower_tolerance: number | null;
    actual_value: number | null;
    is_abnormal: boolean;
  }>;

  const byFeature = new Map<string, FeatureSpecWithValues>();
  for (const r of rows) {
    let entry = byFeature.get(r.feature_name);
    if (!entry) {
      entry = {
        feature_name: r.feature_name,
        spec: {
          nominal_value: Number(r.nominal_value ?? 0),
          upper_tolerance: Number(r.upper_tolerance ?? 0),
          lower_tolerance: Number(r.lower_tolerance ?? 0),
        },
        values: [],
        sample_size: 0,
        total_size: 0,
      };
      byFeature.set(r.feature_name, entry);
    }
    if (r.actual_value != null && Number.isFinite(Number(r.actual_value))) {
      entry.total_size += 1;
      // 已判定異常的點不納入製程能力計算 —— 否則 Cpk 會被特殊原因拉低,
      // 反映的不是製程本身的能力。
      if (r.is_abnormal !== true) {
        entry.values.push(Number(r.actual_value));
        entry.sample_size += 1;
      }
    }
  }

  return Array.from(byFeature.values());
}

/** 每日平均值(生產穩定度折線) + 量測天數 + 最後量測時間 */
export interface DailyAverage {
  date: string; // YYYY-MM-DD
  value: number; // 當日平均
  count: number; // 當日筆數
}

/** 折線圖的聚合粒度 */
export type TrendBucket = "day" | "hour" | "point";

export interface DailySummary {
  points: DailyAverage[];
  /** 實際的不重複量測天數(和折線粒度無關,指標卡用) */
  day_count: number;
  last_measured_at: string | null;
  /** 這次折線用的粒度 */
  bucket: TrendBucket;
}

/**
 * 生產穩定度折線。
 *
 * 粒度自動選:資料若全擠在同一天(事件區間常常只跨幾小時),按日聚合會塌成
 * 一個點,連線都畫不出來。所以先看資料跨度再決定:
 *   跨 ≥ 3 天  → 每日平均
 *   跨 ≥ 3 小時 → 每小時平均
 *   更短        → 不聚合,每筆量測一個點
 */
export async function getDailySummary(
  product: string,
  process: string,
  machine: string,
  featureName: string,
  eventIntervalId: number | null = null,
  eventType: string = DEFAULT_EVENT_TYPE,
): Promise<DailySummary> {
  const sql = getSql();

  // 1) 先看資料跨度與天數
  const spanRows = (await sql`
    SELECT
      COUNT(DISTINCT w."量測時間"::date)::int                  AS day_count,
      COUNT(*)::int                                            AS total,
      EXTRACT(EPOCH FROM (MAX(w."量測時間") - MIN(w."量測時間")))::float8 AS span_seconds,
      to_char(MAX(w."量測時間"), 'YYYY-MM-DD"T"HH24:MI:SS')    AS last_at
    FROM "測量值" m
    JOIN "工件_含事件" w
      ON w."機台"   = m."機台"
     AND w."流水號" = m."流水號"
     AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
    WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
      AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
      AND w."量測時間" IS NOT NULL
      AND (${eventIntervalId}::int IS NULL
           OR w."事件紀錄id" = ${eventIntervalId}::int)
  `) as unknown as Array<{
    day_count: number;
    total: number;
    span_seconds: number | null;
    last_at: string | null;
  }>;

  const meta = spanRows[0];
  const dayCount = Number(meta?.day_count) || 0;
  const total = Number(meta?.total) || 0;
  const spanSeconds = Number(meta?.span_seconds) || 0;
  const lastMeasuredAt = meta?.last_at ?? null;

  if (total === 0) {
    return { points: [], day_count: 0, last_measured_at: null, bucket: "day" };
  }

  const bucket: TrendBucket =
    spanSeconds >= 3 * 86400 ? "day" : spanSeconds >= 3 * 3600 ? "hour" : "point";

  // 2) 不聚合:每筆量測一個點
  if (bucket === "point") {
    const rows = (await sql`
      SELECT
        to_char(w."量測時間", 'MM/DD HH24:MI') AS label,
        m."實際值"::float8                     AS value
      FROM "測量值" m
      JOIN "工件_含事件" w
        ON w."機台"   = m."機台"
       AND w."流水號" = m."流水號"
       AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
      WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
        AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
        AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
        AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
        AND w."量測時間" IS NOT NULL
        AND (${eventIntervalId}::int IS NULL
             OR w."事件紀錄id" = ${eventIntervalId}::int)
      ORDER BY w."量測時間", m."流水號"
    `) as unknown as Array<{ label: string; value: number }>;

    return {
      points: rows.map((r) => ({
        date: r.label,
        value: Math.round(Number(r.value) * 10000) / 10000,
        count: 1,
      })),
      day_count: dayCount,
      last_measured_at: lastMeasuredAt,
      bucket,
    };
  }

  // 3) 按日或按小時聚合
  const unit = bucket === "day" ? "day" : "hour";
  const labelFmt = bucket === "day" ? "MM/DD" : "MM/DD HH24:MI";

  const rows = (await sql`
    SELECT
      to_char(date_trunc(${unit}, w."量測時間"), ${labelFmt}) AS label,
      date_trunc(${unit}, w."量測時間")                        AS bucket_at,
      AVG(m."實際值")::float8                                  AS avg_value,
      COUNT(*)::int                                            AS cnt
    FROM "測量值" m
    JOIN "工件_含事件" w
      ON w."機台"   = m."機台"
     AND w."流水號" = m."流水號"
     AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
    WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
      AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
      AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
      AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
      AND w."量測時間" IS NOT NULL
      AND (${eventIntervalId}::int IS NULL
           OR w."事件紀錄id" = ${eventIntervalId}::int)
    GROUP BY date_trunc(${unit}, w."量測時間")
    ORDER BY date_trunc(${unit}, w."量測時間")
  `) as unknown as Array<{
    label: string;
    avg_value: number;
    cnt: number;
  }>;

  return {
    points: rows.map((r) => ({
      date: r.label,
      value: Math.round(Number(r.avg_value) * 10000) / 10000,
      count: Number(r.cnt) || 0,
    })),
    day_count: dayCount,
    last_measured_at: lastMeasuredAt,
    bucket,
  };
}

/**
 * 把重新算出的製程能力值寫回「管制圖」目前生效的版本。
 *
 * 只更新 active 且在有效期間內的那一筆(和 getFeature 的判定條件一致)。
 * 仍在 Phase I(沒有 active 版本)的尺寸不會被寫入,回傳 updated = 0。
 *
 * 注意:這裡只寫能力欄位,絕不碰六條管制界線。界線只能由核准流程建立。
 */
export interface CapabilityValues {
  cp: number | null;
  cpk: number | null;
  cpm: number | null;
  cpmk: number | null;
  ppk: number | null;
}

// ============================================================================
//  事件區間(原「刀具區間」,已通用化)
//  ---------------------------------------------------------------------------
//  底層表:事件紀錄 (id, 機台, 起始流水號, 事件時間, 事件類型, 備註)
//         事件類型是自由文字(換刀 / 保養 / 參數調整 …),只記「從哪個流水號開始」
//  View:  事件使用區間 (事件紀錄id, 機台, 事件類型, 起始流水號,
//                       結束流水號_不含, 事件時間)
//         已用 LEAD() 補上區間上界(半開區間,NULL = 進行中)
//  View:  工件_含事件 (工件全欄 + 事件類型, 事件紀錄id, 事件啟用時間)
//
//  所以:
//    要列出區間清單          → 讀 事件使用區間(必須帶 事件類型)
//    要把量測值限定在某區間  → JOIN 工件_含事件 再比對 事件紀錄id
//                              (不用自己做流水號範圍 JOIN)
//
//  ⚠️ 事件類型不是可選的篩選條件,是正確性條件:
//     LEAD() 是「在同一個事件類型內」往下找下一筆,不同事件類型各自切各自的
//     區間。查區間清單不帶事件類型 → 換刀和保養的區間會混在同一個下拉;
//     JOIN 工件_含事件 不帶事件類型 → 同一筆量測值會依事件類型數量重複出現。
//
//  區間只按機台切,不分品號 / 製程,同一段區間可能跨多個品號;
//  管制界線一律以 (品號, 製程, 機台, 球標尺寸名, 管制圖類型, 區間) 分開算。
// ============================================================================

/**
 * 列出 DB 裡實際出現過的所有事件類型(給①區下拉用)。
 * 事件類型是自由文字,不能寫死選項,必須從 DB 撈。
 */
export async function listEventTypes(machine?: string | null): Promise<string[]> {
  const sql = getSql();
  const rows = (await sql`
    SELECT DISTINCT TRIM(t."事件類型") AS event_type
      FROM "事件紀錄" t
     WHERE t."事件類型" IS NOT NULL
       AND TRIM(t."事件類型") <> ''
       AND (${machine ?? null}::text IS NULL
            OR NORMALIZE(TRIM(t."機台")) = NORMALIZE(TRIM(${machine ?? null})))
     ORDER BY event_type
  `) as unknown as Array<{ event_type: string }>;
  return rows.map((r) => r.event_type);
}

export interface EventInterval {
  interval_id: number;
  machine: string;
  serial_from: number;
  serial_to_exclusive: number | null;
  changed_at: string | null;
}

/** 列出某機台在指定事件類型下的所有區間,新的在前 */
export async function listEventIntervals(
  machine: string,
  eventType: string = DEFAULT_EVENT_TYPE,
): Promise<EventInterval[]> {
  const sql = getSql();

  const rows = (await sql`
    SELECT
      t."事件紀錄id"        AS interval_id,
      t."機台"              AS machine,
      t."起始流水號"        AS serial_from,
      t."結束流水號_不含"   AS serial_to_exclusive,
      to_char(t."事件時間", 'YYYY-MM-DD"T"HH24:MI:SS') AS changed_at
    FROM "事件使用區間" t
    WHERE NORMALIZE(TRIM(t."機台")) = NORMALIZE(TRIM(${machine}))
      AND NORMALIZE(TRIM(t."事件類型")) = NORMALIZE(TRIM(${eventType}))
    ORDER BY t."起始流水號" DESC
  `) as unknown as Array<{
    interval_id: number;
    machine: string;
    serial_from: number;
    serial_to_exclusive: number | null;
    changed_at: string | null;
  }>;

  return rows.map((r) => ({
    interval_id: Number(r.interval_id),
    machine: r.machine,
    serial_from: Number(r.serial_from),
    serial_to_exclusive:
      r.serial_to_exclusive == null ? null : Number(r.serial_to_exclusive),
    changed_at: r.changed_at ?? null,
  }));
}

/** 舊 API 的相容層；換刀現在是通用事件模型中的預設事件類型。 */
export async function listToolIntervals(
  machine: string,
): Promise<EventInterval[]> {
  return listEventIntervals(machine, DEFAULT_EVENT_TYPE);
}

export interface IntervalSampleStat {
  interval_id: number;
  /** 該區間內的乾淨樣本數(是否異常 ≠ TRUE),取該組合所有尺寸的最小值 */
  clean_sample_count: number;
  /** 累積到第 minSamples 筆乾淨樣本時的量測時間 = 管制開始時間;不足則 null */
  control_start_time: string | null;
  first_measured_at: string | null;
  last_measured_at: string | null;
}

/**
 * 算出某 (品號, 製程, 機台, 球標尺寸名) 在每個事件區間的樣本累積狀況。
 *
 * 管制開始時間的定義:區間內第 minSamples 筆「乾淨樣本」的量測時間。
 * 也就是說,累積到那一刻才算得出管制界線,從那一刻起才進入 Phase II。
 * 樣本不足的區間 control_start_time = null,仍停在 Phase I。
 */
export async function getIntervalSampleStats(
  product: string,
  process: string,
  machine: string,
  featureName: string,
  minSamples: number,
  eventType: string = DEFAULT_EVENT_TYPE,
): Promise<IntervalSampleStat[]> {
  const sql = getSql();

  const rows = (await sql`
    WITH ranked AS (
      SELECT
        w."事件紀錄id"                          AS interval_id,
        w."量測時間"                             AS measured_at,
        ROW_NUMBER() OVER (
          PARTITION BY w."事件紀錄id"
          ORDER BY w."量測時間" NULLS LAST, m."流水號"
        )                                        AS rn
      FROM "測量值" m
      JOIN "工件_含事件" w
        ON w."機台"   = m."機台"
       AND w."流水號" = m."流水號"
       AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
      WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
        AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
        AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
        AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
        AND m."是否異常" = FALSE
        AND w."事件紀錄id" IS NOT NULL
    )
    SELECT
      interval_id,
      COUNT(*)::int                                            AS clean_sample_count,
      to_char(MIN(measured_at), 'YYYY-MM-DD"T"HH24:MI:SS') AS first_measured_at,
      to_char(MAX(measured_at), 'YYYY-MM-DD"T"HH24:MI:SS') AS last_measured_at,
      to_char(MIN(measured_at) FILTER (WHERE rn = ${minSamples}::int), 'YYYY-MM-DD"T"HH24:MI:SS')
                                                               AS control_start_time
    FROM ranked
    GROUP BY interval_id
    ORDER BY interval_id
  `) as unknown as Array<{
    interval_id: number;
    clean_sample_count: number;
    first_measured_at: string | null;
    last_measured_at: string | null;
    control_start_time: string | null;
  }>;

  return rows.map((r) => ({
    interval_id: Number(r.interval_id),
    clean_sample_count: Number(r.clean_sample_count) || 0,
    first_measured_at: r.first_measured_at ?? null,
    last_measured_at: r.last_measured_at ?? null,
    control_start_time: r.control_start_time ?? null,
  }));
}

export async function updateCapabilityValues(
  product: string,
  process: string,
  machine: string,
  featureName: string,
  chartType: ChartType,
  values: CapabilityValues,
  eventIntervalId: number | null = null,
  eventType: string = DEFAULT_EVENT_TYPE,
): Promise<number> {
  const sql = getSql();

  const rows = (await sql`
    WITH span AS (
      SELECT MIN(w."量測時間") AS t0, MAX(w."量測時間") AS t1
      FROM "測量值" m
      JOIN "工件_含事件" w
        ON w."機台"   = m."機台"
       AND w."流水號" = m."流水號"
       AND NORMALIZE(TRIM(w."事件類型")) = NORMALIZE(TRIM(${eventType}))
      WHERE NORMALIZE(TRIM(m."品號"))       = NORMALIZE(TRIM(${product}))
        AND NORMALIZE(TRIM(m."製程"))       = NORMALIZE(TRIM(${process}))
        AND NORMALIZE(TRIM(m."機台"))       = NORMALIZE(TRIM(${machine}))
        AND NORMALIZE(TRIM(m."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
        AND w."事件紀錄id" = ${eventIntervalId}::int
    )
    UPDATE "管制圖" c
       SET "cp"   = ${values.cp},
           "cpk"  = ${values.cpk},
           "cpm"  = ${values.cpm},
           "cpmk" = ${values.cpmk},
           "ppk"  = ${values.ppk}
     WHERE NORMALIZE(TRIM(c."品號"))       = NORMALIZE(TRIM(${product}))
       AND NORMALIZE(TRIM(c."製程"))       = NORMALIZE(TRIM(${process}))
       AND NORMALIZE(TRIM(c."機台"))       = NORMALIZE(TRIM(${machine}))
       AND NORMALIZE(TRIM(c."球標尺寸名")) = NORMALIZE(TRIM(${featureName}))
       AND NORMALIZE(TRIM(c."管制圖類型")) = NORMALIZE(TRIM(${chartType}))
       AND c."管制是否啟用" = TRUE
       AND c."管制開始時間" <= CURRENT_TIMESTAMP
       AND (c."管制結束時間" IS NULL OR c."管制結束時間" > CURRENT_TIMESTAMP)
       AND (
         ${eventIntervalId}::int IS NULL
         OR c."管制開始時間" BETWEEN
              (SELECT t0 FROM span) AND (SELECT t1 FROM span)
       )
    RETURNING 1 AS ok
  `) as unknown as Array<{ ok: number }>;

  return rows.length;
}

/** 儲存既有 Phase I signal 的人工審查結果。 */
export async function saveTrialReview(input: {
  key: TrialReviewKey;
  point_id: string;
  baseline_disposition: BaselineDisposition;
  exclusion_reason_code: ExclusionReasonCode | null;
  exclusion_note: string | null;
  reviewed_by: string | null;
}): Promise<TrialReviewRecord | null> {
  const sql = getSql();
  const reason =
    input.baseline_disposition === "exclude"
      ? input.exclusion_reason_code
      : null;
  const note =
    input.baseline_disposition === "exclude"
      ? input.exclusion_note?.trim() || null
      : null;
  const intervalKey = input.key.tool_interval_id ?? -1;

  const rows = (await sql`
    UPDATE phase_i_trial_review
       SET review_status = 'reviewed',
           baseline_disposition = ${input.baseline_disposition},
           exclusion_reason_code = ${reason},
           exclusion_note = ${note},
           reviewed_by = ${input.reviewed_by?.trim() || null},
           reviewed_at = CURRENT_TIMESTAMP,
           updated_at = CURRENT_TIMESTAMP
     WHERE product = ${input.key.product}
       AND process = ${input.key.process}
       AND machine = ${input.key.machine}
       AND feature_name = ${input.key.feature_name}
       AND chart_type = ${input.key.chart_type}
       AND tool_interval_key = ${intervalKey}
       AND point_id = ${input.point_id}
     RETURNING
       point_id,
       actual_value::float8 AS actual_value,
       trial_violation,
       violated_rules,
       signal_sources,
       review_status,
       baseline_disposition,
       exclusion_reason_code,
       exclusion_note,
       reviewed_by,
       reviewed_at::text AS reviewed_at
  `) as unknown as Array<{
    point_id: string;
    actual_value: number;
    trial_violation: boolean;
    violated_rules: string[];
    signal_sources: TrialReviewRecord["sources"];
    review_status: TrialReviewRecord["review_status"];
    baseline_disposition: BaselineDisposition | null;
    exclusion_reason_code: ExclusionReasonCode | null;
    exclusion_note: string | null;
    reviewed_by: string | null;
    reviewed_at: string | null;
  }>;

  const row = rows[0];
  if (!row) return null;
  return {
    point_id: row.point_id,
    actual_value: Number(row.actual_value),
    trial_violation: row.trial_violation,
    violated_rules: row.violated_rules ?? [],
    sources: row.signal_sources ?? [],
    review_status: row.review_status,
    baseline_disposition: row.baseline_disposition,
    exclusion_reason_code: row.exclusion_reason_code,
    exclusion_note: row.exclusion_note,
    reviewed_by: row.reviewed_by,
    reviewed_at: row.reviewed_at,
  };
}
