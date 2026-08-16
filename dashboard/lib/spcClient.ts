import { splitIntoSubgroups } from "./subgroups";
import type {
  BuildChartDataResponse,
  ChartSeries,
  ChartType,
  FeatureRecord,
  MrChartData,
  MrLimits,
  MrPoint,
  Subgroup,
} from "./types";

const SPC_API_BASE = process.env.SPC_API_BASE ?? "http://127.0.0.1:8000";

function controlLimitForChart(rec: FeatureRecord, chartType: ChartType) {
  const limit = rec.control_limit;
  if (!limit) return null;
  if (limit.chart_type && limit.chart_type !== chartType) return null;
  return {
    ...limit,
    primary_cl: limit.primary_cl ?? limit.cl,
    primary_ucl: limit.primary_ucl ?? limit.ucl,
    primary_lcl: limit.primary_lcl ?? limit.lcl,
  };
}

function buildChartRequestBody(partProcess: string, rec: FeatureRecord) {
  const base = {
    part_process: partProcess,
    feature_name: rec.feature_name,
    spec: rec.spec,
  };

  if (rec.chart_type === "I-MR") {
    return {
      ...base,
      chart_type: "I-MR" as ChartType,
      control_limit: controlLimitForChart(rec, "I-MR"),
      measurements: rec.measurements,
    };
  }

  const subgroups =
    rec.subgroups && rec.subgroups.length > 0
      ? rec.subgroups
      : splitIntoSubgroups(rec.measurements);

  if (subgroups.length < 2) {
    return {
      ...base,
      chart_type: "I-MR" as ChartType,
      control_limit: controlLimitForChart(rec, "I-MR"),
      measurements: rec.measurements,
    };
  }

  return {
    ...base,
    chart_type: rec.chart_type,
    control_limit: controlLimitForChart(rec, rec.chart_type),
    subgroups,
  };
}

export function chartSeriesToMrChartData(
  series: ChartSeries | null | undefined,
): MrChartData | null {
  if (!series) return null;

  const limits =
    series.limits.cl != null &&
    series.limits.ucl != null &&
    series.limits.lcl != null
      ? {
          cl: series.limits.cl,
          ucl: series.limits.ucl,
          lcl: series.limits.lcl,
        }
      : null;

  const points: MrPoint[] = series.points.map((point) => ({
    x: point.x,
    time: point.time,
    value: point.value,
    is_out_of_control: Boolean(point.is_out_of_control),
    violated_rules: point.violated_rules,
  }));

  return { points, limits };
}

export async function buildChartData(
  partProcess: string,
  rec: FeatureRecord,
): Promise<BuildChartDataResponse> {
  const res = await fetch(`${SPC_API_BASE}/spc/build-chart-data`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildChartRequestBody(partProcess, rec)),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`SPC service 回傳 ${res.status}:${text}`);
  }

  return (await res.json()) as BuildChartDataResponse;
}

export async function buildMrChart(
  rec: FeatureRecord,
): Promise<MrChartData | null> {
  if (rec.chart_type !== "I-MR") return null;

  const values = rec.measurements.map((m) => m.actual_value);
  if (values.length < 2) return null;

  const limits: MrLimits | null =
    rec.control_limit?.secondary_cl != null &&
    rec.control_limit.secondary_ucl != null &&
    rec.control_limit.secondary_lcl != null
      ? {
          cl: rec.control_limit.secondary_cl,
          ucl: rec.control_limit.secondary_ucl,
          lcl: rec.control_limit.secondary_lcl,
        }
      : null;

  const points: MrPoint[] = rec.measurements.map((m, i) => {
    const x = String(m.serial_no ?? m.measurement_id ?? i + 1);
    if (i === 0) {
      return {
        x,
        time: m.measured_at,
        value: null,
        is_out_of_control: false,
        violated_rules: [],
      };
    }

    const mr =
      Math.round(Math.abs(values[i] - values[i - 1]) * 10000) / 10000;
    const isOutOfControl = limits ? mr > limits.ucl || mr < limits.lcl : false;
    return {
      x,
      time: m.measured_at,
      value: mr,
      is_out_of_control: isOutOfControl,
      violated_rules: isOutOfControl ? ["above_ucl"] : [],
    };
  });

  return { points, limits };
}

export function getSpcApiBase(): string {
  return SPC_API_BASE;
}

export interface AnalyzeMeasurementResult {
  measurement_id: number | string;
  chart_type: string;
  spec_check: {
    usl: number;
    lsl: number;
    is_out_of_spec: boolean;
    spec_violation: "above_usl" | "below_lsl" | "within_spec";
  };
  control_check: {
    has_active_control_limit: boolean;
    cl: number | null;
    ucl: number | null;
    lcl: number | null;
    is_out_of_control: boolean | null;
    violated_rules: string[];
  };
  capability: {
    cp: number | null;
    cpk: number | null;
    cpm?: number | null;
    cpmk?: number | null;
    ppk?: number | null;
  };
  trigger: {
    should_create_abnormal_event: boolean;
    should_alert: boolean;
    should_call_ai_summary: boolean;
    should_call_rag: boolean;
    severity: "normal" | "medium" | "high";
    event_type:
      | "normal"
      | "out_of_spec"
      | "out_of_control"
      | "out_of_spec_and_out_of_control";
  };
}

export async function analyzeMeasurement(
  rec: FeatureRecord,
  newMeasurement: FeatureRecord["measurements"][number],
): Promise<AnalyzeMeasurementResult> {
  const body = {
    measurement: {
      measurement_id: newMeasurement.measurement_id,
      actual_value: newMeasurement.actual_value,
      measured_at: newMeasurement.measured_at,
      serial_no: newMeasurement.serial_no,
    },
    spec: rec.spec,
    control_limit: rec.control_limit,
    history: rec.measurements.map((m) => ({
      measurement_id: m.measurement_id,
      actual_value: m.actual_value,
    })),
  };

  const res = await fetch(`${SPC_API_BASE}/spc/analyze-measurement`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`SPC service 回傳 ${res.status}:${text}`);
  }
  return (await res.json()) as AnalyzeMeasurementResult;
}

export interface TrialLimitsResult {
  cl: number;
  ucl: number;
  lcl: number;
  sample_size: number;
  subgroup_size: number | null;
  trial_has_abnormal_points: boolean;
  abnormal_points: Array<{
    measurement_id: number | string;
    actual_value: number;
    violated_rules: string[];
  }>;
  bottom_chart: { cl: number; ucl: number; lcl: number } | null;
}

export async function calculateTrialLimits(
  featureId: number | string,
  chartType: ChartType,
  args:
    | { measurements: FeatureRecord["measurements"] }
    | { subgroups: Subgroup[] },
): Promise<TrialLimitsResult> {
  const body: Record<string, unknown> = {
    chart_type: chartType,
    feature_id: featureId,
  };

  if (chartType === "I-MR") {
    if (!("measurements" in args)) {
      throw new Error("I-MR 需要 measurements 陣列");
    }
    body.measurements = args.measurements.map((m) => ({
      measurement_id: m.measurement_id,
      actual_value: m.actual_value,
    }));
  } else {
    if (!("subgroups" in args)) {
      throw new Error(`${chartType} 需要 subgroups 陣列`);
    }
    body.subgroups = args.subgroups;
  }

  const res = await fetch(`${SPC_API_BASE}/spc/calculate-trial-limits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`SPC service 回傳 ${res.status}:${text}`);
  }

  const j = (await res.json()) as {
    cl: number;
    ucl: number;
    lcl: number;
    sample_size: number;
    subgroup_size?: number | null;
    trial_has_abnormal_points: boolean;
    abnormal_points?: Array<{
      measurement_id: number | string;
      actual_value: number;
      violated_rules: string[];
    }>;
    mr_chart?: { cl: number; ucl: number; lcl: number } | null;
    range_chart?: { cl: number; ucl: number; lcl: number } | null;
    s_chart?: { cl: number; ucl: number; lcl: number } | null;
  };

  const bottom =
    chartType === "I-MR"
      ? j.mr_chart ?? null
      : chartType === "Xbar-R"
      ? j.range_chart ?? null
      : j.s_chart ?? null;

  return {
    cl: j.cl,
    ucl: j.ucl,
    lcl: j.lcl,
    sample_size: j.sample_size,
    subgroup_size: j.subgroup_size ?? null,
    trial_has_abnormal_points: j.trial_has_abnormal_points,
    abnormal_points: j.abnormal_points ?? [],
    bottom_chart: bottom,
  };
}

export interface CapabilityResult {
  cp: number | null;
  cpk: number | null;
  cpm: number | null;
  cpmk: number | null;
  ppk: number | null;
}

/**
 * @deprecated 改用 calculateCapabilityDetail()。
 * 這支沒有帶 target_value,Python 會用 (USL+LSL)/2 當目標值,
 * 上下公差不對稱時算出的 Cpm / Cpmk 會和儀表板顯示的不一致。
 */
export async function calculateCapability(
  featureId: number | string,
  featureName: string,
  spec: FeatureRecord["spec"],
  measurements: number[],
): Promise<CapabilityResult> {
  const res = await fetch(`${SPC_API_BASE}/spc/capability`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      feature_id: featureId,
      feature_name: featureName,
      spec,
      measurements,
    }),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`SPC service 回傳 ${res.status}:${text}`);
  }

  const payload = (await res.json()) as CapabilityResult;
  return {
    cp: payload.cp ?? null,
    cpk: payload.cpk ?? null,
    cpm: payload.cpm ?? null,
    cpmk: payload.cpmk ?? null,
    ppk: payload.ppk ?? null,
  };
}

// ────────────────────────────────────────────────────────────────────────────
//  儀表板用:完整製程能力(含 mean / sigma / usl / lsl / sample_size)
//  target_value 明確帶入「定義值」,避免上下公差不對稱時 Cpm/Cpmk 用錯中心。
// ────────────────────────────────────────────────────────────────────────────

export interface CapabilityDetail extends CapabilityResult {
  sample_size: number;
  mean: number | null;
  sigma: number | null;
  usl: number;
  lsl: number;
}

export async function calculateCapabilityDetail(
  featureId: number | string,
  featureName: string,
  spec: FeatureRecord["spec"],
  measurements: number[],
): Promise<CapabilityDetail> {
  const res = await fetch(`${SPC_API_BASE}/spc/capability`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      feature_id: featureId,
      feature_name: featureName,
      spec,
      measurements,
      target_value: spec.nominal_value,
    }),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`SPC service 回傳 ${res.status}:${text}`);
  }

  const payload = (await res.json()) as Partial<CapabilityDetail>;
  return {
    sample_size: Number(payload.sample_size ?? 0),
    mean: payload.mean ?? null,
    sigma: payload.sigma ?? null,
    usl: Number(payload.usl ?? 0),
    lsl: Number(payload.lsl ?? 0),
    cp: payload.cp ?? null,
    cpk: payload.cpk ?? null,
    cpm: payload.cpm ?? null,
    cpmk: payload.cpmk ?? null,
    ppk: payload.ppk ?? null,
  };
}
