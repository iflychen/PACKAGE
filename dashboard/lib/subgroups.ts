// ============================================================================
//  Subgroup 分組 — Xbar-R / Xbar-S 需要子組
//  ---------------------------------------------------------------------------
//  策略:按時間順序,連續 n 筆一組,不足 n 筆的尾巴丟掉。
//    例:15 筆量測 + size=5  → 3 個 subgroup
//        13 筆量測 + size=5  → 2 個 subgroup(最後 3 筆丟)
// ============================================================================

import type { Measurement, Subgroup } from "./types";

/** 預設子組大小(可之後改成 config) */
export const DEFAULT_SUBGROUP_SIZE = 5;

/** Python 對 Xbar-R/S 的最少子組數(常見值 3~5,取 3 較寬鬆) */
export const MIN_SUBGROUPS = 3;

/**
 * 把時間排好的 measurements 按 n 筆一組切成 subgroups。
 * 首筆的 serial_no 或 measurement_id 當 subgroup_id。
 */
export function splitIntoSubgroups(
  measurements: Measurement[],
  size: number = DEFAULT_SUBGROUP_SIZE,
): Subgroup[] {
  const out: Subgroup[] = [];
  for (let i = 0; i + size <= measurements.length; i += size) {
    const chunk = measurements.slice(i, i + size);
    const first = chunk[0];
    const id = first.serial_no ?? String(first.measurement_id ?? `SG${i / size + 1}`);
    out.push({
      subgroup_id: id,
      values: chunk.map((m) => m.actual_value),
    });
  }
  return out;
}
