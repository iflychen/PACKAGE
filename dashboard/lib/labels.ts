// 把 Python 回傳的 violated_rules 代碼轉成中文說明。
// 規則代碼來自 app/spc.py 的 detect_control_rule_violations / check_spec_violation。

export const RULE_LABELS: Record<string, string> = {
  // 規格（spec）違反
  above_usl: "超出規格上限 USL",
  below_lsl: "低於規格下限 LSL",
  // 基本管制界線違反
  above_ucl: "超出管制上限 UCL",
  below_lcl: "低於管制下限 LCL",
  // Western Electric 規則
  western_2_of_3_above_2sigma: "連續3點中有2點超出 +2σ",
  western_2_of_3_below_2sigma: "連續3點中有2點低於 -2σ",
  western_4_of_5_above_1sigma: "連續5點中有4點超出 +1σ",
  western_4_of_5_below_1sigma: "連續5點中有4點低於 -1σ",
  // Nelson 連串規則
  nelson_8_points_above_center: "連續8點在中心線上方",
  nelson_8_points_below_center: "連續8點在中心線下方",
};

export function ruleLabel(code: string): string {
  return RULE_LABELS[code] ?? code;
}

// 三種狀態：超規(紅) > 失控(橘) > 正常(綠)
export type PointStatus = "out_of_spec" | "out_of_control" | "normal";

export function pointStatus(p: {
  is_out_of_spec: boolean;
  is_out_of_control: boolean | null;
}): PointStatus {
  if (p.is_out_of_spec) return "out_of_spec";
  if (p.is_out_of_control) return "out_of_control";
  return "normal";
}

export const STATUS_COLOR: Record<PointStatus, string> = {
  out_of_spec: "#dc2626", // 紅
  out_of_control: "#f59e0b", // 橘
  normal: "#16a34a", // 綠
};

export const STATUS_LABEL: Record<PointStatus, string> = {
  out_of_spec: "超出規格（異常值）",
  out_of_control: "管制失控（異常值）",
  normal: "標準值（正常）",
};
