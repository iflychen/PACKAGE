// ============================================================================
//  單筆量測「事後處理」核心邏輯 — 新 schema (品號/製程/機台/球標尺寸名)
//  ---------------------------------------------------------------------------
//  被 /api/measurements/ingest 和 /api/measurements/notify 共用。
// ============================================================================

import { getFeature } from "./db";
import { getSql } from "./neon";
import {
  analyzeMeasurement,
  type AnalyzeMeasurementResult,
} from "./spcClient";
import type { ChartType } from "./types";

export interface ProcessMeasurementInput {
  product: string;      // 品號
  process: string;      // 製程
  machine: string;      // 機台
  serial_no: number;    // 流水號
  feature_name: string; // 球標尺寸名
  chart_type?: ChartType; // 預設 I-MR
}

export interface ProcessMeasurementResult {
  ok: true;
  phase: "I" | "II";
  status:
    | "phase_ii_analyzed"
    | "phase_ii_limits_refined"
    | "accumulating"
    | "trial_needs_review"
    | "control_limits_activated";
  product: string;
  process: string;
  machine: string;
  feature_name: string;
  chart_type: ChartType;
  serial_no: number;
  is_abnormal: boolean;
  anomaly_type: string | null;
  sample_count: number;
  clean_sample_count?: number;
  control_limits?: { cl: number; ucl: number; lcl: number };
  analysis: AnalyzeMeasurementResult;
  note?: string;
}

function classifyAnomaly(
  isOutOfSpec: boolean,
  isOutOfControl: boolean | null,
): { is_abnormal: boolean; anomaly_type: string | null } {
  if (isOutOfSpec && isOutOfControl) {
    return { is_abnormal: true, anomaly_type: "超規且失控" };
  }
  if (isOutOfSpec) return { is_abnormal: true, anomaly_type: "超規" };
  if (isOutOfControl) return { is_abnormal: true, anomaly_type: "失控" };
  return { is_abnormal: false, anomaly_type: null };
}

export async function processMeasurement(
  input: ProcessMeasurementInput,
): Promise<ProcessMeasurementResult> {
  const {
    product,
    process,
    machine,
    serial_no,
    feature_name,
    chart_type = "I-MR",
  } = input;

  // 1) 讀 spec / active control_limit / 全歷史
  const rec = await getFeature(
    product,
    process,
    machine,
    feature_name,
    chart_type,
  );
  if (!rec) {
    throw new Error(
      `找不到球標尺寸「${feature_name}」(品號=${product}, 製程=${process})`,
    );
  }
  const newMeas = rec.measurements.find((m) => m.serial_no === serial_no);
  if (!newMeas) {
    throw new Error(`讀不到量測值 serial_no=${serial_no};可能外部 INSERT 尚未 commit`);
  }

  // 2) 呼叫 Python 分析
  const analysis = await analyzeMeasurement(rec, newMeas);
  const hasActive = analysis.control_check.has_active_control_limit;
  const isOutOfSpec = analysis.spec_check.is_out_of_spec;
  const isOutOfControl = analysis.control_check.is_out_of_control;

  const { is_abnormal, anomaly_type } = classifyAnomaly(
    isOutOfSpec,
    hasActive ? isOutOfControl : false,
  );

  // 3) 更新剛才這筆的 是否異常 / 異常類型
  const sql = getSql();
  await sql`
    UPDATE "測量值"
       SET "是否異常" = ${is_abnormal},
           "異常類型" = ${anomaly_type}
     WHERE "品號"       = ${product}
       AND "製程"       = ${process}
       AND "機台"       = ${machine}
       AND "流水號"     = ${serial_no}
       AND "球標尺寸名" = ${feature_name}
  `;

  const totalSampleCount = rec.measurements.length;

  // Phase II
  if (hasActive) {
    return {
      ok: true,
      phase: "II",
      status: "phase_ii_analyzed",
      product,
      process,
      machine,
      feature_name,
      chart_type,
      serial_no,
      is_abnormal,
      anomaly_type,
      sample_count: totalSampleCount,
      analysis,
      note: "沿用 DB active 管制線完成 Phase II 判定，未重新試算。",
    };
  }

  // Phase I 只累積資料；試算、排除與核准由使用者在畫面主動執行。
  return {
    ok: true,
    phase: "I",
    status: "accumulating",
    product,
    process,
    machine,
    feature_name,
    chart_type,
    serial_no,
    is_abnormal,
    anomaly_type,
    sample_count: totalSampleCount,
    analysis,
    note: "尚未建立管制界線；請由使用者執行 Phase I 試算與核准。",
  };
}
