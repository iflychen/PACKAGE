from __future__ import annotations

from math import sqrt

from fastapi import FastAPI, HTTPException

from app.ai_summary import AiSummaryRequest, AiSummaryResponse, generate_ai_summary
from app.llm import LlmError
from app.models import (
    AbnormalTrialPointResponse,
    AnalyzeMeasurementRequest,
    AnalyzeMeasurementResponse,
    BuildChartDataRequest,
    BuildChartDataResponse,
    CalculateTrialLimitsRequest,
    CalculateTrialLimitsResponse,
    CapabilityRequest,
    CapabilityResponse,
    CapabilitySummaryResponse,
    ChartLimitsResponse,
    ChartPointResponse,
    ChartSeriesResponse,
    ControlCheckResponse,
    ControlLimitInput,
    ControlLimitComponentResponse,
    MrChartResponse,
    SpecCheckResponse,
    SubgroupInput,
    TriggerResponse,
)
from app.spc import (
    SpecLimits,
    build_trigger,
    calculate_cp_cpk,
    calculate_imr_limits,
    calculate_spec_limits,
    calculate_xbar_r_limits,
    calculate_xbar_s_limits,
    check_spec_violation,
    detect_control_limit_violations,
    detect_control_rule_violations,
)


app = FastAPI(
    title="Python SPC Service",
    version="0.1.0",
    description="Backend-only SPC calculation and judgement service for iPQC.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/spc/ai-summary", response_model=AiSummaryResponse)
async def ai_summary(request: AiSummaryRequest) -> AiSummaryResponse:
    try:
        return await generate_ai_summary(request)
    except LlmError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/spc/analyze-measurement", response_model=AnalyzeMeasurementResponse)   # (路徑, 回傳格式)
def analyze_measurement(
    request: AnalyzeMeasurementRequest,
) -> AnalyzeMeasurementResponse:
    spec_limits = calculate_spec_limits(
        request.spec.nominal_value,
        request.spec.upper_tolerance,
        request.spec.lower_tolerance,
    )
    spec_violation = check_spec_violation(
        request.measurement.actual_value,
        spec_limits,
    )
    is_out_of_spec = spec_violation != "within_spec"

    control_check = _build_control_check(
        values=[item.actual_value for item in request.history]
        + [request.measurement.actual_value],
        control_limit=request.control_limit,
    )

    values = [item.actual_value for item in request.history]
    values.append(request.measurement.actual_value)
    capability = calculate_cp_cpk(values, spec_limits)
    trigger = build_trigger(is_out_of_spec, control_check.is_out_of_control)

    return AnalyzeMeasurementResponse(
        measurement_id=request.measurement.measurement_id,
        chart_type=request.control_limit.chart_type if request.control_limit else "I-MR",
        spec_check=SpecCheckResponse(
            usl=spec_limits.usl,
            lsl=spec_limits.lsl,
            is_out_of_spec=is_out_of_spec,
            spec_violation=spec_violation,
        ),
        control_check=control_check,
        capability=CapabilitySummaryResponse(
            cp=capability.cp,
            cpk=capability.cpk,
            cpm=capability.cpm,
            cpmk=capability.cpmk,
            ppk=capability.ppk,
        ),
        trigger=TriggerResponse(**trigger.__dict__),
    )


@app.post("/spc/calculate-trial-limits", response_model=CalculateTrialLimitsResponse)
def calculate_trial_limits(
    request: CalculateTrialLimitsRequest,
) -> CalculateTrialLimitsResponse:
    if request.chart_type == "Xbar-R":
        return _calculate_xbar_trial_limits(request, request.subgroups)
    if request.chart_type == "Xbar-S":
        return _calculate_xbar_trial_limits(request, request.subgroups)

    excluded = set(request.excluded_measurement_ids)
    included_measurements = [
        item for item in request.measurements if item.measurement_id not in excluded
    ]
    values = [item.actual_value for item in included_measurements]

    try:
        limits = calculate_imr_limits(values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    rule_results = detect_control_rule_violations(values, limits.cl, limits.ucl, limits.lcl)
    abnormal_points: list[AbnormalTrialPointResponse] = []
    for item, violated_rules in zip(included_measurements, rule_results):
        if violated_rules:
            abnormal_points.append(
                AbnormalTrialPointResponse(
                    measurement_id=item.measurement_id,
                    actual_value=item.actual_value,
                    violated_rules=violated_rules,
                )
            )

    return CalculateTrialLimitsResponse(
        feature_id=request.feature_id,
        chart_type=request.chart_type,
        cl=limits.cl,
        ucl=limits.ucl,
        lcl=limits.lcl,
        sample_size=limits.sample_size,
        trial_has_abnormal_points=bool(abnormal_points),
        abnormal_points=abnormal_points,
        mr_chart=MrChartResponse(
            cl=limits.mr_cl,
            ucl=limits.mr_ucl,
            lcl=limits.mr_lcl,
        ),
        primary_component="I",
        primary_limit=MrChartResponse(cl=limits.cl, ucl=limits.ucl, lcl=limits.lcl),
        secondary_component="MR",
        secondary_limit=MrChartResponse(
            cl=limits.mr_cl,
            ucl=limits.mr_ucl,
            lcl=limits.mr_lcl,
        ),
        components=[
            ControlLimitComponentResponse(
                component_type="I",
                cl=limits.cl,
                ucl=limits.ucl,
                lcl=limits.lcl,
            ),
            ControlLimitComponentResponse(
                component_type="MR",
                cl=limits.mr_cl,
                ucl=limits.mr_ucl,
                lcl=limits.mr_lcl,
            ),
        ],
    )


@app.post("/spc/build-chart-data", response_model=BuildChartDataResponse)
def build_chart_data(request: BuildChartDataRequest) -> BuildChartDataResponse:
    spec_limits = calculate_spec_limits(
        request.spec.nominal_value,
        request.spec.upper_tolerance,
        request.spec.lower_tolerance,
    )

    primary_chart = _build_primary_chart_series(request, spec_limits)
    secondary_chart = _build_secondary_chart_series(request)

    return BuildChartDataResponse(
        part_process=request.part_process,
        feature_name=request.feature_name,
        chart_type=request.chart_type,
        limits=ChartLimitsResponse(
            cl=request.control_limit.primary_cl if request.control_limit else None,
            ucl=request.control_limit.primary_ucl if request.control_limit else None,
            lcl=request.control_limit.primary_lcl if request.control_limit else None,
            usl=spec_limits.usl,
            lsl=spec_limits.lsl,
        ),
        points=primary_chart.points,
        primary_chart=primary_chart,
        secondary_chart=secondary_chart,
    )


@app.post("/spc/capability", response_model=CapabilityResponse)
def calculate_capability(request: CapabilityRequest) -> CapabilityResponse:
    spec_limits = calculate_spec_limits(
        request.spec.nominal_value,
        request.spec.upper_tolerance,
        request.spec.lower_tolerance,
    )
    capability = calculate_cp_cpk(
        request.measurements,
        spec_limits,
        target_value=request.target_value,
        long_term_sigma=request.long_term_sigma,
    )

    return CapabilityResponse(
        feature_id=request.feature_id,
        feature_name=request.feature_name,
        sample_size=capability.sample_size,
        mean=capability.mean,
        sigma=capability.sigma,
        usl=capability.usl,
        lsl=capability.lsl,
        cp=capability.cp,
        cpk=capability.cpk,
        cpm=capability.cpm,
        cpmk=capability.cpmk,
        ppk=capability.ppk,
    )


def _build_control_check(
    values: list[float],
    control_limit: ControlLimitInput | None
) -> ControlCheckResponse:
    if not control_limit or not control_limit.is_active:
        return ControlCheckResponse(
            has_active_control_limit=False,
            is_out_of_control=None,
            violated_rules=[],
        )

    violated_rules = detect_control_rule_violations(
        values,
        control_limit.primary_cl,
        control_limit.primary_ucl,
        control_limit.primary_lcl,
    )[-1]
    return ControlCheckResponse(
        has_active_control_limit=True,
        control_limit_version_id=control_limit.version_id,
        cl=control_limit.primary_cl,
        ucl=control_limit.primary_ucl,
        lcl=control_limit.primary_lcl,
        is_out_of_control=bool(violated_rules),
        violated_rules=violated_rules,
    )


def _calculate_xbar_trial_limits(
    request: CalculateTrialLimitsRequest,
    subgroups: list[SubgroupInput],
) -> CalculateTrialLimitsResponse:
    subgroup_values = [item.values for item in subgroups]
    try:
        limits = (
            calculate_xbar_r_limits(subgroup_values) if request.chart_type == "Xbar-R"
            else calculate_xbar_s_limits(subgroup_values)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    subgroup_means = [sum(item.values) / len(item.values) for item in subgroups]
    rule_results = detect_control_rule_violations(
        subgroup_means,
        limits.cl,
        limits.ucl,
        limits.lcl,
    )
    abnormal_points: list[AbnormalTrialPointResponse] = []
    for subgroup, subgroup_mean, violated_rules in zip(
        subgroups,
        subgroup_means,
        rule_results,
    ):
        if violated_rules:
            abnormal_points.append(
                AbnormalTrialPointResponse(
                    measurement_id=subgroup.subgroup_id,
                    actual_value=subgroup_mean,
                    violated_rules=violated_rules,
                )
            )

    secondary_chart = MrChartResponse(
        cl=limits.secondary_cl,
        ucl=limits.secondary_ucl,
        lcl=limits.secondary_lcl,
    )
    return CalculateTrialLimitsResponse(
        feature_id=request.feature_id,
        chart_type=request.chart_type,
        cl=limits.cl,
        ucl=limits.ucl,
        lcl=limits.lcl,
        sample_size=limits.sample_size,
        subgroup_size=limits.subgroup_size,
        trial_has_abnormal_points=bool(abnormal_points),
        abnormal_points=abnormal_points,
        range_chart=secondary_chart if request.chart_type == "Xbar-R" else None,
        s_chart=secondary_chart if request.chart_type == "Xbar-S" else None,
        primary_component="XBAR",
        primary_limit=MrChartResponse(cl=limits.cl, ucl=limits.ucl, lcl=limits.lcl),
        secondary_component=limits.secondary_chart_name,
        secondary_limit=secondary_chart,
        components=[
            ControlLimitComponentResponse(
                component_type="XBAR",
                cl=limits.cl,
                ucl=limits.ucl,
                lcl=limits.lcl,
            ),
            ControlLimitComponentResponse(
                component_type=limits.secondary_chart_name,
                cl=limits.secondary_cl,
                ucl=limits.secondary_ucl,
                lcl=limits.secondary_lcl,
            ),
        ],
    )


def _build_primary_chart_series(
    request: BuildChartDataRequest,
    spec_limits: SpecLimits,
) -> ChartSeriesResponse:
    if request.chart_type in {"Xbar-R", "Xbar-S"}:
        values = [sum(subgroup.values) / len(subgroup.values) for subgroup in request.subgroups]
        point_inputs = [
            (
                str(subgroup.subgroup_id),
                subgroup.measured_at,
                value,
            )
            for subgroup, value in zip(request.subgroups, values)
        ]
        component_type = "XBAR"
    else:
        values = [measurement.actual_value for measurement in request.measurements]
        point_inputs = [
            (
                str(measurement.serial_no)
                if measurement.serial_no is not None
                else str(measurement.measurement_id or index),
                measurement.measured_at,
                measurement.actual_value,
            )
            for index, measurement in enumerate(request.measurements, start=1)
        ]
        component_type = "I"

    limits = _primary_chart_limits(request, spec_limits)
    points = _build_chart_points(
        point_inputs,
        values,
        spec_limits=spec_limits,
        cl=limits.cl,
        ucl=limits.ucl,
        lcl=limits.lcl,
        control_limit_active=bool(request.control_limit and request.control_limit.is_active),
        include_run_rules=True,
    )

    return ChartSeriesResponse(
        component_type=component_type,
        limits=limits,
        points=points,
    )


def _build_secondary_chart_series(
    request: BuildChartDataRequest,
) -> ChartSeriesResponse | None:
    if not request.control_limit or not request.control_limit.is_active:
        return None
    if (
        request.control_limit.secondary_cl is None
        or request.control_limit.secondary_ucl is None
        or request.control_limit.secondary_lcl is None
    ):
        return None

    if request.chart_type == "I-MR":
        if len(request.measurements) < 2:
            return None
        values = [
            abs(current.actual_value - previous.actual_value)
            for previous, current in zip(request.measurements, request.measurements[1:])
        ]
        point_inputs = [
            (
                str(current.serial_no)
                if current.serial_no is not None
                else str(current.measurement_id or index),
                current.measured_at,
                value,
            )
            for index, (current, value) in enumerate(zip(request.measurements[1:], values), start=2)
        ]
        component_type = "MR"
    elif request.chart_type == "Xbar-R":
        values = [max(subgroup.values) - min(subgroup.values) for subgroup in request.subgroups]
        point_inputs = [
            (str(subgroup.subgroup_id), subgroup.measured_at, value)
            for subgroup, value in zip(request.subgroups, values)
        ]
        component_type = "R"
    else:
        values = [_sample_standard_deviation(subgroup.values) for subgroup in request.subgroups]
        point_inputs = [
            (str(subgroup.subgroup_id), subgroup.measured_at, value)
            for subgroup, value in zip(request.subgroups, values)
        ]
        component_type = "S"

    limits = ChartLimitsResponse(
        cl=request.control_limit.secondary_cl,
        ucl=request.control_limit.secondary_ucl,
        lcl=request.control_limit.secondary_lcl,
    )
    points = _build_chart_points(
        point_inputs,
        values,
        spec_limits=None,
        cl=limits.cl,
        ucl=limits.ucl,
        lcl=limits.lcl,
        control_limit_active=True,
        include_run_rules=False,
    )
    return ChartSeriesResponse(
        component_type=component_type,
        limits=limits,
        points=points,
    )


def _primary_chart_limits(
    request: BuildChartDataRequest,
    spec_limits: SpecLimits,
) -> ChartLimitsResponse:
    return ChartLimitsResponse(
        cl=request.control_limit.primary_cl if request.control_limit else None,
        ucl=request.control_limit.primary_ucl if request.control_limit else None,
        lcl=request.control_limit.primary_lcl if request.control_limit else None,
        usl=spec_limits.usl,
        lsl=spec_limits.lsl,
    )


def _build_chart_points(
    point_inputs: list[tuple[str, str | None, float]],
    values: list[float],
    spec_limits: SpecLimits | None,
    cl: float | None,
    ucl: float | None,
    lcl: float | None,
    control_limit_active: bool,
    include_run_rules: bool,
) -> list[ChartPointResponse]:
    if control_limit_active and cl is not None and ucl is not None and lcl is not None:
        rule_results = (
            detect_control_rule_violations(values, cl, ucl, lcl)
            if include_run_rules
            else detect_control_limit_violations(values, ucl, lcl)
        )
    else:
        rule_results = [[] for _ in values]

    points: list[ChartPointResponse] = []
    for (x_value, measured_at, value), control_rules in zip(point_inputs, rule_results):
        if spec_limits is not None:
            spec_violation = check_spec_violation(value, spec_limits)
            spec_rules = [] if spec_violation == "within_spec" else [spec_violation]
        else:
            spec_rules = []

        points.append(
            ChartPointResponse(
                x=x_value,
                time=measured_at,
                value=value,
                is_out_of_spec=bool(spec_rules),
                is_out_of_control=bool(control_rules) if control_limit_active else None,
                violated_rules=[*spec_rules, *control_rules],
            )
        )
    return points


def _sample_standard_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    x_bar = sum(values) / len(values)
    variance = sum((value - x_bar) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance)
