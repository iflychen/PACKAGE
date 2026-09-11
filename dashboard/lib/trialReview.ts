import type { ChartType } from "./types";

export type ReviewStatus = "pending" | "reviewed";
export type BaselineDisposition = "retain" | "exclude";
export type ExclusionReasonCode =
  | "tool_issue"
  | "machine_adjustment"
  | "measurement_error"
  | "material_issue"
  | "fixture_positioning"
  | "startup_first_piece"
  | "operator_error"
  | "other";

export const EXCLUSION_REASON_OPTIONS: Array<{
  value: ExclusionReasonCode;
  label: string;
}> = [
  { value: "tool_issue", label: "刀具異常 / 刀具更換" },
  { value: "machine_adjustment", label: "機台調整" },
  { value: "measurement_error", label: "量測錯誤" },
  { value: "material_issue", label: "材料異常" },
  { value: "fixture_positioning", label: "治具 / 定位異常" },
  { value: "startup_first_piece", label: "換線 / 開機 / 首件" },
  { value: "operator_error", label: "操作錯誤" },
  { value: "other", label: "其他" },
];

export interface TrialReviewKey {
  product: string;
  process: string;
  machine: string;
  feature_name: string;
  chart_type: ChartType;
  tool_interval_id: number | null;
}

export interface TrialSignal {
  point_id: string;
  actual_value: number;
  violated_rules: string[];
  sources: Array<{
    chart: "primary" | "secondary";
    component_type: string;
  }>;
}

export interface TrialReviewRecord extends TrialSignal {
  trial_violation: boolean;
  review_status: ReviewStatus;
  baseline_disposition: BaselineDisposition | null;
  exclusion_reason_code: ExclusionReasonCode | null;
  exclusion_note: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
}

export function validateReviewDisposition(input: {
  baseline_disposition: unknown;
  exclusion_reason_code?: unknown;
  exclusion_note?: unknown;
}): string | null {
  if (input.baseline_disposition !== "retain" && input.baseline_disposition !== "exclude") {
    return "處理方式必須是保留於基準計算或排除於基準計算。";
  }
  if (input.baseline_disposition === "retain") return null;

  const validReasons = new Set(EXCLUSION_REASON_OPTIONS.map((item) => item.value));
  if (
    typeof input.exclusion_reason_code !== "string" ||
    !validReasons.has(input.exclusion_reason_code as ExclusionReasonCode)
  ) {
    return "排除於基準計算時必須選擇排除原因。";
  }
  if (
    input.exclusion_reason_code === "other" &&
    (typeof input.exclusion_note !== "string" || !input.exclusion_note.trim())
  ) {
    return "排除原因選擇其他時，補充說明不能為空。";
  }
  return null;
}
