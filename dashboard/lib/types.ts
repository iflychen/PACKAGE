// 與 Python SPC service 對齊的型別定義。
// 回應型別對應 app/models.py 的 BuildChartDataResponse。

export type ChartType = "I-MR" | "Xbar-R" | "Xbar-S";

// ============================================================================
//  事件類型
//  ---------------------------------------------------------------------------
//  DB 的「換刀紀錄」已通用化為「事件紀錄」,事件類型是自由文字(換刀 / 保養 /
//  參數調整 …),由使用者自己填。
//
//  ⚠️ 為什麼每個查詢都一定要帶事件類型:
//     view「工件_含事件」對同一件工件,每有一種事件類型就會多出一列。
//     JOIN 時若不限定事件類型,量測值會被乘上事件類型的數量 ——
//     Cpk 會偏移、樣本數會虛增、管制開始時間會提前,而且完全不會報錯。
//     所以 db.ts 裡每一個 JOIN "工件_含事件" 的 ON 條件都必須帶事件類型。
//
//  預設用「換刀」,維持通用化之前的行為。
// ============================================================================
export const DEFAULT_EVENT_TYPE = "換刀";

// ---- Python /spc/build-chart-data 的「回應」格式 ----
export interface ChartLimits {
  cl: number | null;
  ucl: number | null;
  lcl: number | null;
  usl: number | null;
  lsl: number | null;
}

export interface ChartPoint {
  x: string;
  time: string | null;
  value: number;
  is_out_of_spec: boolean;
  is_out_of_control: boolean | null;
  violated_rules: string[];
}

export interface BuildChartDataResponse {
  part_process: string; // Python 那邊沿用舊欄名(其實裝的是 品號::製程::機台)
  feature_name: string;
  chart_type: ChartType;
  limits: ChartLimits;
  points: ChartPoint[];
  primary_chart?: ChartSeries | null;
  secondary_chart?: ChartSeries | null;
}

export type ChartComponentType = "I" | "MR" | "XBAR" | "R" | "S";

export interface ChartSeries {
  component_type: ChartComponentType;
  limits: ChartLimits;
  points: ChartPoint[];
}

// ---- 移動全距(MR)圖 ----
export interface MrLimits {
  cl: number;
  ucl: number;
  lcl: number;
}

export interface MrPoint {
  x: string;
  time?: string | null;
  value: number | null;
  is_out_of_control: boolean;
  violated_rules?: string[];
}

export interface MrChartData {
  points: MrPoint[];
  limits: MrLimits | null;
}

// /api/chart 回傳:個別值圖 + 移動全距圖 + 定位資訊
export interface ChartApiResponse extends BuildChartDataResponse {
  mr: MrChartData | null;
  /** DB 目前是否存在符合完整 key 且生效中的 active 版本。 */
  has_active_control_limit?: boolean;
  /** DB active 版本的正式界線；Phase I 時為 null。 */
  active_control_limit?: ControlLimit | null;
  // 新 schema 定位欄位(冗餘寫回,方便前端顯示):
  product?: string;
  process?: string;
  machine?: string;
  event_interval_id?: number | null;
  event_type?: string;
}

// ---- 對應 Neon schema 的資料模型 ----
export interface Spec {
  nominal_value: number;
  upper_tolerance: number;
  lower_tolerance: number;
}

export interface ControlLimit {
  chart_type?: ChartType;
  cl: number;
  ucl: number;
  lcl: number;
  primary_cl?: number;
  primary_ucl?: number;
  primary_lcl?: number;
  secondary_cl?: number | null;
  secondary_ucl?: number | null;
  secondary_lcl?: number | null;
  is_active: boolean;
}

export interface Measurement {
  measurement_id: number | string;
  actual_value: number;
  measured_at?: string;
  serial_no?: number;
  /**
   * 測量值.是否異常 —— 系統先前判定過的異常點。
   *
   * 這種點「要畫出來但不能拿來當基準」:管制圖上必須看得到它,
   * 但建立管制界線和算製程能力時都要排除,否則界線會被異常點拉寬。
   */
  is_abnormal?: boolean;
}

export interface Subgroup {
  subgroup_id: number | string;
  values: number[];
  measured_at?: string;
}

/** 定位某個管制對象:品號 + 製程 + 機台 + 球標尺寸名 (+ 管制圖類型) */
export interface FeatureKey {
  product: string;      // 品號
  process: string;      // 製程
  machine: string;      // 機台
  feature_name: string; // 球標尺寸名
  chart_type: ChartType; // 管制圖類型 (預設 I-MR)
}

export interface FeatureRecord extends FeatureKey {
  feature_id: string; // 派生:`${product}::${process}::${machine}::${feature_name}`
  spec: Spec;
  control_limit: ControlLimit | null;
  /** 完整 key 有 active DB row；即使該 row 的界線欄位不完整也不可重建。 */
  has_active_control_limit?: boolean;
  measurements: Measurement[];
  subgroups?: Subgroup[];
}

// ---- /api/processes 給前端下拉選單用的「扁平組合清單」 ----
// 每一筆代表一個 (品號, 製程, 機台, 球標尺寸名) 組合 (只包含有測量值的組合)。
// 前端會用它衍生四層級聯下拉的候選項。
export interface FeatureCombo {
  product: string;
  process: string;
  machine: string;
  feature_name: string;
  chart_type: ChartType;
  has_active_control_limit: boolean;
  sample_size: number;
}

export interface ProcessesResponse {
  combos: FeatureCombo[];
}
