from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator
                    # 限制型別  #欄位規則 #整個model規則

ChartType = Literal["I-MR", "Xbar-R", "Xbar-S"]


class MeasurementInput(BaseModel):
    measurement_id: int | str
    actual_value: float
    measured_at: str | None = None
    serial_no: int | None = None


class SpecInput(BaseModel):
    nominal_value: float
    upper_tolerance: float
    lower_tolerance: float

    @model_validator(mode="after")              # 填完欄位後驗證，確保上限大於下限
    def validate_limits(self) -> "SpecInput":
        if self.upper_tolerance < self.lower_tolerance:
            raise ValueError("upper_tolerance must be greater than lower_tolerance")
        return self


class ControlLimitInput(BaseModel):
    version_id: int | None = None
    chart_type: ChartType = "Xbar-R"              # 預設 Xbar-R
    cl: float | None = None
    ucl: float | None = None
    lcl: float | None = None
    primary_cl: float | None = None
    primary_ucl: float | None = None
    primary_lcl: float | None = None
    secondary_cl: float | None = None
    secondary_ucl: float | None = None
    secondary_lcl: float | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def validate_order(self) -> "ControlLimitInput":
        if self.primary_cl is None:
            self.primary_cl = self.cl
        if self.primary_ucl is None:
            self.primary_ucl = self.ucl
        if self.primary_lcl is None:
            self.primary_lcl = self.lcl
        if self.cl is None:
            self.cl = self.primary_cl
        if self.ucl is None:
            self.ucl = self.primary_ucl
        if self.lcl is None:
            self.lcl = self.primary_lcl

        if self.primary_cl is None or self.primary_ucl is None or self.primary_lcl is None:
            raise ValueError("primary control limits are required")
        if self.primary_ucl < self.primary_lcl:
            raise ValueError("primary_ucl must be greater than or equal to primary_lcl")

        secondary_values = [self.secondary_cl, self.secondary_ucl, self.secondary_lcl]
        if any(value is not None for value in secondary_values):
            if any(value is None for value in secondary_values):
                raise ValueError("secondary control limits must be provided together")
            if self.secondary_ucl < self.secondary_lcl:
                raise ValueError("secondary_ucl must be greater than or equal to secondary_lcl")
        return self


class SubgroupInput(BaseModel):
    subgroup_id: int | str
    values: list[float]
    measured_at: str | None = None


class AnalyzeMeasurementRequest(BaseModel):                         # 用於分析測量結果的請求模型，包含測量值、規格、控制限制和歷史測量值
    measurement: MeasurementInput
    spec: SpecInput
    control_limit: ControlLimitInput | None = None
    history: list[MeasurementInput] = Field(default_factory=list)


class SpecCheckResponse(BaseModel):
    usl: float
    lsl: float
    is_out_of_spec: bool
    spec_violation: Literal["above_usl", "below_lsl", "within_spec"]


class ControlCheckResponse(BaseModel):
    has_active_control_limit: bool
    control_limit_version_id: int | None = None
    cl: float | None = None
    ucl: float | None = None
    lcl: float | None = None
    is_out_of_control: bool | None = None
    violated_rules: list[str] = Field(default_factory=list)


class CapabilitySummaryResponse(BaseModel):
    cp: float | None
    cpk: float | None
    cpm: float | None = None
    cpmk: float | None = None
    ppk: float | None = None

class TriggerResponse(BaseModel):
    should_create_abnormal_event: bool
    should_alert: bool
    should_call_ai_summary: bool
    should_call_rag: bool
    severity: Literal["normal", "medium", "high"]
    event_type: Literal[
        "normal",
        "out_of_spec",
        "out_of_control",
        "out_of_spec_and_out_of_control",
    ]


class AnalyzeMeasurementResponse(BaseModel):       # 分析測量結果的綜合回應模型，包含測量ID、圖表類型、規格檢查結果、控制檢查結果、能力摘要和觸發決策
    measurement_id: int | str
    chart_type: ChartType
    spec_check: SpecCheckResponse
    control_check: ControlCheckResponse
    capability: CapabilitySummaryResponse
    trigger: TriggerResponse


class CalculateTrialLimitsRequest(BaseModel):
    chart_type: ChartType = "Xbar-R"
    feature_id: int | str
    measurements: list[MeasurementInput] = Field(default_factory=list)
    subgroups: list[SubgroupInput] = Field(default_factory=list)
    excluded_measurement_ids: list[int | str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_chart_payload(self) -> "CalculateTrialLimitsRequest":
        if self.chart_type == "I-MR" and not self.measurements:
            raise ValueError("measurements are required for I-MR")
        if self.chart_type in {"Xbar-R", "Xbar-S"} and not self.subgroups:
            raise ValueError("subgroups are required for Xbar-R and Xbar-S")
        return self


class MrChartResponse(BaseModel):
    cl: float
    ucl: float
    lcl: float


class ControlLimitComponentResponse(BaseModel):
    component_type: Literal["I", "MR", "XBAR", "R", "S"]
    cl: float
    ucl: float
    lcl: float


class AbnormalTrialPointResponse(BaseModel):         # 表示試驗控制圖中的異常點
    measurement_id: int | str
    actual_value: float
    violated_rules: list[str]


class CalculateTrialLimitsResponse(BaseModel):       # 試驗控制圖的限制值，並返回相關資訊，包括異常點和控制圖數據
    feature_id: int | str
    chart_type: ChartType
    cl: float
    ucl: float
    lcl: float
    sample_size: int
    subgroup_size: int | None = None
    trial_has_abnormal_points: bool
    abnormal_points: list[AbnormalTrialPointResponse]
    mr_chart: MrChartResponse | None = None
    range_chart: MrChartResponse | None = None
    s_chart: MrChartResponse | None = None
    primary_component: Literal["I", "XBAR"]
    primary_limit: MrChartResponse
    secondary_component: Literal["MR", "R", "S"]
    secondary_limit: MrChartResponse
    components: list[ControlLimitComponentResponse]


class BuildChartDataRequest(BaseModel):               # 用於構建控制圖數據的請求模型，包含零件過程、特徵名稱、圖表類型、規格、控制限制和測量值
    part_process: str
    feature_name: str
    chart_type: ChartType = "Xbar-R"
    spec: SpecInput
    control_limit: ControlLimitInput | None = None
    measurements: list[MeasurementInput] = Field(default_factory=list)
    subgroups: list[SubgroupInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_chart_payload(self) -> "BuildChartDataRequest":
        if self.chart_type == "I-MR" and not self.measurements:
            raise ValueError("measurements are required for I-MR")
        if self.chart_type in {"Xbar-R", "Xbar-S"} and not self.subgroups:
            raise ValueError("subgroups are required for Xbar-R and Xbar-S")
        return self


class ChartLimitsResponse(BaseModel):                  # 提供前端控制圖的限制值，包括中心線、上控制限、下控制限、上規格限和下規格限
    cl: float | None
    ucl: float | None
    lcl: float | None
    usl: float | None = None
    lsl: float | None = None


class ChartPointResponse(BaseModel):                    # 提供前端點資訊
    x: str
    time: str | None                                    # 測量時間
    value: float
    is_out_of_spec: bool
    is_out_of_control: bool | None
    violated_rules: list[str]


class ChartSeriesResponse(BaseModel):
    component_type: Literal["I", "MR", "XBAR", "R", "S"]
    limits: ChartLimitsResponse
    points: list[ChartPointResponse]


class BuildChartDataResponse(BaseModel):                # 提供前端控制圖數據的模型
    part_process: str
    feature_name: str
    chart_type: ChartType
    limits: ChartLimitsResponse
    points: list[ChartPointResponse]
    primary_chart: ChartSeriesResponse
    secondary_chart: ChartSeriesResponse | None = None


class CapabilityRequest(BaseModel):
    feature_id: int | str
    feature_name: str
    spec: SpecInput
    measurements: list[float]
    target_value: float | None = None
    long_term_sigma: float | None = None


class CapabilityResponse(BaseModel):
    feature_id: int | str
    feature_name: str
    sample_size: int
    mean: float | None
    sigma: float | None
    usl: float
    lsl: float
    cp: float | None
    cpk: float | None
    cpm: float | None = None
    cpmk: float | None = None
    ppk: float | None = None
