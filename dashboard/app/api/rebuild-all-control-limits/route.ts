// POST /api/rebuild-all-control-limits
//
// 首頁載入時自動呼叫。新邏輯(不再每次全表重算):
//
//   對每個 (品號, 製程, 機台, 球標尺寸名) × (I-MR / Xbar-R / Xbar-S):
//     1) 先查「管制圖」是否已有啟用的一筆(Phase II)
//        → 有,而且六界線齊全 → 直接沿用 DB 內的界線,不再計算 (status=kept_phase_ii)
//     2) 沒有 (仍在 Phase I) → 檢查乾淨樣本數
//        → 樣本足夠:呼叫 Python 計算 trial limits,INSERT 新的一筆並啟用 (status=inserted)
//        → 樣本不足:維持 Phase I,不動 DB (status=skipped_insufficient_samples)
//
// 一/二 欄位語意:
//   I-MR   → 一=I chart,  二=MR chart
//   Xbar-R → 一=Xbar chart,二=R chart
//   Xbar-S → 一=Xbar chart,二=S chart

import { NextResponse } from "next/server";
import { getSql } from "@/lib/neon";
import { calculateTrialLimits, getSpcApiBase } from "@/lib/spcClient";
import type { ChartType, Measurement } from "@/lib/types";
import { getMinSamples } from "@/lib/config";
import {
  DEFAULT_SUBGROUP_SIZE,
  MIN_SUBGROUPS,
  splitIntoSubgroups,
} from "@/lib/subgroups";

export const dynamic = "force-dynamic";

const CHART_TYPES: ChartType[] = ["I-MR", "Xbar-R", "Xbar-S"];

type ComboRow = {
  product: string;
  process: string;
  machine: string;
  feature_name: string;
};

type MeasurementRow = {
  measurement_id: number;
  serial_no: number;
  actual_value: number;
};

type ExistingRow = {
  cl: number | null;
  ucl: number | null;
  lcl: number | null;
  is_active: boolean | string | null;
};

interface FeatureResult {
  product: string;
  process: string;
  machine: string;
  feature_name: string;
  chart_type: ChartType;
  status:
    | "kept_phase_ii"
    | "inserted"
    | "skipped_insufficient_samples"
    | "skipped_python_error"
    | "skipped_db_error";
  clean_sample_count: number;
  subgroups_used?: number;
  limits?: { cl: number; ucl: number; lcl: number };
  error?: string;
  note?: string;
}

function isActiveFlag(value: unknown): boolean {
  return (
    value === true ||
    value === "t" ||
    value === "true" ||
    value === "TRUE" ||
    value === 1
  );
}

export async function POST() {
  const minSamples = getMinSamples();
  let sql;
  try {
    sql = getSql();
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "連接資料庫來源錯誤", detail },
      { status: 500 },
    );
  }

  let combos: ComboRow[];
  try {
    combos = (await sql`
      SELECT DISTINCT
        m."品號"        AS product,
        m."製程"        AS process,
        m."機台"        AS machine,
        m."球標尺寸名"  AS feature_name
      FROM "測量值" m
      ORDER BY product, process, machine, feature_name
    `) as unknown as ComboRow[];
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "讀取組合失敗", detail },
      { status: 500 },
    );
  }

  const results: FeatureResult[] = [];

  for (const c of combos) {
    // ────────────────────────────────────────────────────────
    // Step 1: 先看三種 chart type 各自在「管制圖」內有沒有啟用的一筆
    // ────────────────────────────────────────────────────────
    const existingByChart: Record<
      ChartType,
      { cl: number; ucl: number; lcl: number } | null
    > = {
      "I-MR": null,
      "Xbar-R": null,
      "Xbar-S": null,
    };

    let existErr: string | null = null;
    for (const ct of CHART_TYPES) {
      try {
        const rows = (await sql`
          SELECT
            "管制中線一"::float8 AS cl,
            "管制上界一"::float8 AS ucl,
            "管制下界一"::float8 AS lcl,
            "管制是否啟用"       AS is_active
          FROM "管制圖"
           WHERE NORMALIZE(TRIM("品號"))       = NORMALIZE(TRIM(${c.product}))
             AND NORMALIZE(TRIM("製程"))       = NORMALIZE(TRIM(${c.process}))
             AND NORMALIZE(TRIM("機台"))       = NORMALIZE(TRIM(${c.machine}))
             AND NORMALIZE(TRIM("球標尺寸名")) = NORMALIZE(TRIM(${c.feature_name}))
             AND NORMALIZE(TRIM("管制圖類型")) = NORMALIZE(TRIM(${ct}))
           LIMIT 1
        `) as unknown as ExistingRow[];

        if (rows.length > 0) {
          const row = rows[0];
          if (
            isActiveFlag(row.is_active) &&
            row.cl != null &&
            row.ucl != null &&
            row.lcl != null
          ) {
            existingByChart[ct] = {
              cl: Number(row.cl),
              ucl: Number(row.ucl),
              lcl: Number(row.lcl),
            };
          }
        }
      } catch (err) {
        existErr = err instanceof Error ? err.message : String(err);
        break;
      }
    }

    if (existErr) {
      for (const ct of CHART_TYPES) {
        results.push({
          ...c,
          chart_type: ct,
          status: "skipped_db_error",
          clean_sample_count: 0,
          error: existErr,
        });
      }
      continue;
    }

    // Step 2: 對「已有啟用管制圖」的 chart type 直接沿用
    for (const ct of CHART_TYPES) {
      const kept = existingByChart[ct];
      if (kept) {
        results.push({
          ...c,
          chart_type: ct,
          status: "kept_phase_ii",
          clean_sample_count: 0,
          limits: kept,
          note: "DB 已有啟用管制界線,直接沿用",
        });
      }
    }

    // Step 3: 對「還沒有啟用管制圖」的 chart type 才嘗試建立(Phase I → II)
    const needsRebuild = CHART_TYPES.filter((ct) => existingByChart[ct] === null);
    if (needsRebuild.length === 0) continue;

    // 撈乾淨樣本(只有需要建立時才讀,避免無謂 DB I/O)
    let cleanRows: MeasurementRow[];
    try {
      cleanRows = (await sql`
        SELECT
          m."流水號"          AS measurement_id,
          m."流水號"          AS serial_no,
          m."實際值"::float8  AS actual_value
        FROM "測量值" m
        LEFT JOIN "工件" w
          ON w."機台"   = m."機台"
         AND w."流水號" = m."流水號"
        WHERE m."品號"       = ${c.product}
          AND m."製程"       = ${c.process}
          AND m."機台"       = ${c.machine}
          AND m."球標尺寸名" = ${c.feature_name}
          AND (m."是否異常" IS DISTINCT FROM TRUE)
        ORDER BY w."量測時間" NULLS LAST, m."流水號"
      `) as unknown as MeasurementRow[];
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      for (const ct of needsRebuild) {
        results.push({
          ...c,
          chart_type: ct,
          status: "skipped_db_error",
          clean_sample_count: 0,
          error: detail,
        });
      }
      continue;
    }

    const measurements: Measurement[] = cleanRows.map((r) => ({
      measurement_id: Number(r.measurement_id),
      serial_no: Number(r.serial_no),
      actual_value: Number(r.actual_value),
    }));

    for (const chartType of needsRebuild) {
      let trial;
      let subgroupsUsed: number | undefined = undefined;

      try {
        if (chartType === "I-MR") {
          if (cleanRows.length < minSamples) {
            results.push({
              ...c,
              chart_type: chartType,
              status: "skipped_insufficient_samples",
              clean_sample_count: cleanRows.length,
              note: `Phase I 累積中 (${cleanRows.length}/${minSamples})`,
            });
            continue;
          }

          trial = await calculateTrialLimits(
            `${c.product}::${c.process}::${c.machine}::${c.feature_name}`,
            "I-MR",
            { measurements },
          );
        } else {
          const subgroups = splitIntoSubgroups(
            measurements,
            DEFAULT_SUBGROUP_SIZE,
          );
          subgroupsUsed = subgroups.length;

          if (subgroups.length < MIN_SUBGROUPS) {
            results.push({
              ...c,
              chart_type: chartType,
              status: "skipped_insufficient_samples",
              clean_sample_count: cleanRows.length,
              subgroups_used: subgroups.length,
              note:
                `Phase I 累積中 (子組 ${subgroups.length}/${MIN_SUBGROUPS},` +
                `每組 ${DEFAULT_SUBGROUP_SIZE} 筆)`,
            });
            continue;
          }

          trial = await calculateTrialLimits(
            `${c.product}::${c.process}::${c.machine}::${c.feature_name}`,
            chartType,
            { subgroups },
          );
        }
      } catch (err) {
        results.push({
          ...c,
          chart_type: chartType,
          status: "skipped_python_error",
          clean_sample_count: cleanRows.length,
          subgroups_used: subgroupsUsed,
          error: err instanceof Error ? err.message : String(err),
        });
        continue;
      }

      const bottomCl = trial.bottom_chart?.cl ?? null;
      const bottomUcl = trial.bottom_chart?.ucl ?? null;
      const bottomLcl = trial.bottom_chart?.lcl ?? null;

      // 這裡一律 INSERT — 前面已確認 DB 沒有啟用的一筆
      try {
        await sql`
          INSERT INTO "管制圖"
            ("品號", "製程", "機台", "球標尺寸名", "管制圖類型",
             "管制中線一", "管制上界一", "管制下界一",
             "管制中線二", "管制上界二", "管制下界二",
             "管制是否啟用")
          VALUES
            (${c.product}, ${c.process}, ${c.machine}, ${c.feature_name}, ${chartType},
             ${trial.cl}, ${trial.ucl}, ${trial.lcl},
             ${bottomCl}, ${bottomUcl}, ${bottomLcl},
             TRUE)
        `;
        results.push({
          ...c,
          chart_type: chartType,
          status: "inserted",
          clean_sample_count: cleanRows.length,
          subgroups_used: subgroupsUsed,
          limits: { cl: trial.cl, ucl: trial.ucl, lcl: trial.lcl },
          note: "樣本足夠,已建立管制界線並進入 Phase II",
        });
      } catch (err) {
        results.push({
          ...c,
          chart_type: chartType,
          status: "skipped_db_error",
          clean_sample_count: cleanRows.length,
          subgroups_used: subgroupsUsed,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
  }

  const summary = {
    total: results.length,
    kept: results.filter((r) => r.status === "kept_phase_ii").length,
    inserted: results.filter((r) => r.status === "inserted").length,
    skipped_insufficient: results.filter(
      (r) => r.status === "skipped_insufficient_samples",
    ).length,
    skipped_python_error: results.filter(
      (r) => r.status === "skipped_python_error",
    ).length,
    skipped_db_error: results.filter((r) => r.status === "skipped_db_error").length,
    by_chart_type: {
      "I-MR": results.filter(
        (r) =>
          r.chart_type === "I-MR" &&
          (r.status === "kept_phase_ii" || r.status === "inserted"),
      ).length,
      "Xbar-R": results.filter(
        (r) =>
          r.chart_type === "Xbar-R" &&
          (r.status === "kept_phase_ii" || r.status === "inserted"),
      ).length,
      "Xbar-S": results.filter(
        (r) =>
          r.chart_type === "Xbar-S" &&
          (r.status === "kept_phase_ii" || r.status === "inserted"),
      ).length,
    },
  };

  return NextResponse.json({
    ok: true,
    min_samples: minSamples,
    subgroup_size: DEFAULT_SUBGROUP_SIZE,
    min_subgroups: MIN_SUBGROUPS,
    spc_api_base: getSpcApiBase(),
    summary,
    results,
  });
}
