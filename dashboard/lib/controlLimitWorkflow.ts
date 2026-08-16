import { getMinSamples } from "./config";
import { getFeature } from "./db";
import { getSql } from "./neon";
import {
  buildChartData,
  calculateCapabilityDetail,
  calculateTrialLimits,
  chartSeriesToMrChartData,
  type TrialLimitsResult,
} from "./spcClient";
import {
  DEFAULT_SUBGROUP_SIZE,
  MIN_SUBGROUPS,
  splitIntoSubgroups,
} from "./subgroups";
import type {
  ChartApiResponse,
  ChartType,
  ControlLimit,
  FeatureRecord,
  Measurement,
  Subgroup,
} from "./types";
import { DEFAULT_EVENT_TYPE } from "./types";

export interface ControlLimitSelection {
  product: string;
  process: string;
  machine: string;
  feature_name: string;
  chart_type: ChartType;
  excluded_point_ids: Array<number | string>;
  /** 事件區間;null 代表不分區間(該機台在此事件類型下沒有紀錄) */
  event_interval_id?: number | null;
  /** 事件類型(換刀 / 保養 / …);省略時用 DEFAULT_EVENT_TYPE */
  event_type?: string;
}

export interface FeatureTrialResult {
  selection: Omit<ControlLimitSelection, "excluded_point_ids">;
  sample_count: number;
  subgroup_count: number | null;
  excluded_point_ids: Array<number | string>;
  trial: TrialLimitsResult;
  chart: ChartApiResponse;
  spec: FeatureRecord["spec"];
  capability_measurements: Measurement[];
  /**
   * 管制開始時間 = 建立界線所用的最後一筆樣本的量測時間。
   * 累積到這一刻才算得出界線,從這一刻起進入 Phase II。
   * 樣本沒有量測時間時退回 null(核准時會改用當下時間)。
   */
  control_start_time: string | null;
}

export class ControlLimitWorkflowError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function samePointId(
  value: number | string | undefined,
  excluded: Set<string>,
): boolean {
  return value != null && excluded.has(String(value));
}

function temporaryControlLimit(
  chartType: ChartType,
  trial: TrialLimitsResult,
): ControlLimit {
  return {
    chart_type: chartType,
    cl: trial.cl,
    ucl: trial.ucl,
    lcl: trial.lcl,
    primary_cl: trial.cl,
    primary_ucl: trial.ucl,
    primary_lcl: trial.lcl,
    secondary_cl: trial.bottom_chart?.cl ?? null,
    secondary_ucl: trial.bottom_chart?.ucl ?? null,
    secondary_lcl: trial.bottom_chart?.lcl ?? null,
    is_active: true,
  };
}

function filterSubgroups(
  measurements: Measurement[],
  excluded: Set<string>,
): { subgroups: Subgroup[]; capabilityMeasurements: Measurement[] } {
  const allSubgroups = splitIntoSubgroups(measurements, DEFAULT_SUBGROUP_SIZE);
  const keptIds = new Set(
    allSubgroups
      .filter((subgroup) => !samePointId(subgroup.subgroup_id, excluded))
      .map((subgroup) => String(subgroup.subgroup_id)),
  );
  const subgroups = allSubgroups.filter((subgroup) =>
    keptIds.has(String(subgroup.subgroup_id)),
  );

  const capabilityMeasurements: Measurement[] = [];
  for (
    let index = 0;
    index + DEFAULT_SUBGROUP_SIZE <= measurements.length;
    index += DEFAULT_SUBGROUP_SIZE
  ) {
    const chunk = measurements.slice(index, index + DEFAULT_SUBGROUP_SIZE);
    const subgroupId =
      chunk[0].serial_no ?? chunk[0].measurement_id ?? `SG${index + 1}`;
    if (keptIds.has(String(subgroupId))) capabilityMeasurements.push(...chunk);
  }
  return { subgroups, capabilityMeasurements };
}

export async function calculateFeatureTrial(
  input: ControlLimitSelection,
): Promise<FeatureTrialResult> {
  const eventIntervalId = input.event_interval_id ?? null;
  const eventType = input.event_type ?? DEFAULT_EVENT_TYPE;

  const rec = await getFeature(
    input.product,
    input.process,
    input.machine,
    input.feature_name,
    input.chart_type,
    eventIntervalId,
    eventType,
  );
  if (!rec) {
    throw new ControlLimitWorkflowError("找不到指定的品號／製程／尺寸資料。", 404);
  }
  if (rec.has_active_control_limit || rec.control_limit) {
    throw new ControlLimitWorkflowError(
      "此尺寸已有 active 管制線，請直接使用 DB 管制線，不可重新試算。",
      409,
    );
  }

  const excluded = new Set(input.excluded_point_ids.map(String));
  let trial: TrialLimitsResult;
  let chartRecord: FeatureRecord;
  let capabilityMeasurements: Measurement[];
  let subgroupCount: number | null = null;
  // 建立界線所用的最後一筆基準樣本 → 決定管制開始時間
  let baselineLastMeasurement: Measurement | undefined;

  if (input.chart_type === "I-MR") {
    // 使用者手動勾選排除的點:整個拿掉(圖上也不畫)
    const kept = rec.measurements.filter(
      (measurement) =>
        !samePointId(measurement.measurement_id, excluded) &&
        !samePointId(measurement.serial_no, excluded),
    );
    // 系統已判定異常的點:圖上要畫出來,但不能當基準也不算能力
    const clean = kept.filter((measurement) => measurement.is_abnormal !== true);

    const minimum = getMinSamples();
    if (clean.length < minimum) {
      throw new ControlLimitWorkflowError(
        `Phase I 至少需要 ${minimum} 筆乾淨樣本；` +
          `排除後只剩 ${clean.length} 筆(總計 ${kept.length} 筆,其中 ` +
          `${kept.length - clean.length} 筆已判定異常)。`,
        422,
      );
    }
    // 界線只用「前 N 筆乾淨樣本」算,之後的點一律拿這組固定界線來監控。
    const baseline = clean.slice(0, minimum);
    baselineLastMeasurement = baseline[baseline.length - 1];

    trial = await calculateTrialLimits(rec.feature_id, "I-MR", {
      measurements: baseline,
    });
    capabilityMeasurements = clean;
    chartRecord = {
      ...rec,
      // 圖上畫全部(含異常點),否則看不到異常在哪
      measurements: kept,
      control_limit: temporaryControlLimit("I-MR", trial),
    };
  } else {
    // Xbar:子組由乾淨樣本切分。異常點會打亂子組邊界,
    // 標準做法也是整組排除而非挑單點。
    const cleanForSubgroups = rec.measurements.filter(
      (measurement) => measurement.is_abnormal !== true,
    );
    const filtered = filterSubgroups(cleanForSubgroups, excluded);
    subgroupCount = filtered.subgroups.length;
    if (filtered.subgroups.length < MIN_SUBGROUPS) {
      throw new ControlLimitWorkflowError(
        `${input.chart_type} 至少需要 ${MIN_SUBGROUPS} 組，每組 ${DEFAULT_SUBGROUP_SIZE} 筆；排除後只剩 ${filtered.subgroups.length} 組。`,
        422,
      );
    }
    // 同樣只用前 MIN_SUBGROUPS 組算界線
    const baselineSubgroups = filtered.subgroups.slice(0, MIN_SUBGROUPS);
    const baselineSize = MIN_SUBGROUPS * DEFAULT_SUBGROUP_SIZE;
    baselineLastMeasurement =
      filtered.capabilityMeasurements[baselineSize - 1] ??
      filtered.capabilityMeasurements[filtered.capabilityMeasurements.length - 1];

    trial = await calculateTrialLimits(rec.feature_id, input.chart_type, {
      subgroups: baselineSubgroups,
    });
    capabilityMeasurements = filtered.capabilityMeasurements;
    chartRecord = {
      ...rec,
      subgroups: filtered.subgroups,
      control_limit: temporaryControlLimit(input.chart_type, trial),
    };
  }

  const chartData = await buildChartData(
    `${input.product}::${input.process}::${input.machine}`,
    chartRecord,
  );
  const chart: ChartApiResponse = {
    ...chartData,
    mr: chartSeriesToMrChartData(chartData.secondary_chart),
    product: input.product,
    process: input.process,
    machine: input.machine,
  };

  return {
    selection: {
      product: input.product,
      process: input.process,
      machine: input.machine,
      feature_name: input.feature_name,
      chart_type: input.chart_type,
      event_interval_id: eventIntervalId,
      event_type: eventType,
    },
    sample_count: capabilityMeasurements.length,
    subgroup_count: subgroupCount,
    excluded_point_ids: input.excluded_point_ids,
    trial,
    chart,
    spec: rec.spec,
    capability_measurements: capabilityMeasurements,
    control_start_time: baselineLastMeasurement?.measured_at ?? null,
  };
}

export async function approveFeatureTrial(
  result: FeatureTrialResult,
): Promise<void> {
  const sql = getSql();
  const { selection, trial } = result;
  // 用和儀表板同一支計算函式(會帶 target_value = 定義值),
  // 避免上下公差不對稱時,存進 DB 的 Cpm/Cpmk 和畫面顯示的不一致。
  const capability = await calculateCapabilityDetail(
    `${selection.product}::${selection.process}::${selection.machine}::${selection.feature_name}`,
    selection.feature_name,
    result.spec,
    result.capability_measurements.map((measurement) => measurement.actual_value),
  );
  // 管制開始時間 = 累積到足夠樣本的那一筆量測時間(不是核准當下的時間)。
  // 樣本沒有量測時間時才退回 now(),避免寫入 NULL 讓 active 判定失準。
  const startedAt = result.control_start_time ?? new Date().toISOString();
  const secondaryCl = trial.bottom_chart?.cl ?? null;
  const secondaryUcl = trial.bottom_chart?.ucl ?? null;
  const secondaryLcl = trial.bottom_chart?.lcl ?? null;

  // 管制圖 PK = (品號, 製程, 機台, 球標尺寸名, 管制圖類型, 管制開始時間),
  // 而 管制開始時間 對同一區間是固定值(第 N 筆樣本的量測時間)。
  // 所以「核准過的區間改排除點後再核准」一定會撞 PK → 必須用 upsert。
  await sql`
    INSERT INTO "管制圖"
      ("品號", "製程", "機台", "球標尺寸名", "管制圖類型",
       "管制開始時間", "管制結束時間",
       "管制中線一", "管制上界一", "管制下界一",
       "管制中線二", "管制上界二", "管制下界二",
       "管制是否啟用",
       "cp", "cpk", "cpm", "cpmk", "ppk")
    VALUES
      (${selection.product}, ${selection.process}, ${selection.machine},
       ${selection.feature_name}, ${selection.chart_type},
       ${startedAt}, NULL,
       ${trial.cl}, ${trial.ucl}, ${trial.lcl},
       ${secondaryCl}, ${secondaryUcl}, ${secondaryLcl},
       TRUE,
       ${capability.cp}, ${capability.cpk}, ${capability.cpm},
       ${capability.cpmk}, ${capability.ppk})
    ON CONFLICT ("品號", "製程", "機台", "球標尺寸名", "管制圖類型", "管制開始時間")
    DO UPDATE SET
      "管制中線一"   = EXCLUDED."管制中線一",
      "管制上界一"   = EXCLUDED."管制上界一",
      "管制下界一"   = EXCLUDED."管制下界一",
      "管制中線二"   = EXCLUDED."管制中線二",
      "管制上界二"   = EXCLUDED."管制上界二",
      "管制下界二"   = EXCLUDED."管制下界二",
      "管制結束時間" = NULL,
      "管制是否啟用" = TRUE,
      "cp"           = EXCLUDED."cp",
      "cpk"          = EXCLUDED."cpk",
      "cpm"          = EXCLUDED."cpm",
      "cpmk"         = EXCLUDED."cpmk",
      "ppk"          = EXCLUDED."ppk"
  `;

  // 只有「不分區間」模式(該機台在此事件類型下沒有紀錄)才關掉更早的版本 ——
  // 那種情況一個組合同時只該有一組 active 界線。
  //
  // 有事件區間時絕不能關:每個區間是一張獨立的管制圖,舊區間的版本要一直
  // 保持 active,使用者切回舊區間才看得到當時的界線。關掉會讓它變回 Phase I。
  if (selection.event_interval_id == null) {
    await sql`
      UPDATE "管制圖"
         SET "管制是否啟用" = FALSE,
             "管制結束時間" = ${startedAt}
       WHERE "品號" = ${selection.product}
         AND "製程" = ${selection.process}
         AND "機台" = ${selection.machine}
         AND "球標尺寸名" = ${selection.feature_name}
         AND "管制圖類型" = ${selection.chart_type}
         AND "管制開始時間" < ${startedAt}
         AND ("管制結束時間" IS NULL OR "管制結束時間" > ${startedAt})
    `;
  }
}
