from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean
from typing import Iterable, Literal


SpecViolation = Literal["above_usl", "below_lsl", "within_spec"]     # 規格違反類型
ControlViolation = Literal["above_ucl", "below_lcl"]                 # 控制違反類型，只考慮單點，複雜規則由 detect_control_rule_violations 處理
Severity = Literal["normal", "medium", "high"]                       # 根據嚴重程度分類事件
EventType = Literal[
    "normal",
    "out_of_spec",
    "out_of_control",
    "out_of_spec_and_out_of_control",
]


@dataclass(frozen=True)
class SpecLimits:
    usl: float
    lsl: float


@dataclass(frozen=True)
class ImrLimits:
    cl: float
    ucl: float
    lcl: float
    mr_cl: float
    mr_ucl: float
    mr_lcl: float
    sample_size: int


@dataclass(frozen=True)
class SubgroupLimits:
    chart_type: Literal["Xbar-R", "Xbar-S"]
    cl: float
    ucl: float
    lcl: float
    secondary_chart_name: Literal["R", "S"]
    secondary_cl: float
    secondary_ucl: float
    secondary_lcl: float
    sample_size: int
    subgroup_size: int



@dataclass(frozen=True)
class Capability:
    sample_size: int
    mean: float | None
    sigma: float | None
    usl: float
    lsl: float
    cp: float | None
    cpk: float | None
    cpm: float | None
    cpmk: float | None
    ppk: float | None


@dataclass(frozen=True)
class TriggerDecision:
    should_create_abnormal_event: bool
    should_alert: bool
    should_call_ai_summary: bool
    should_call_rag: bool
    severity: Severity
    event_type: EventType


XBAR_R_CONSTANTS: dict[int, tuple[float, float, float]] = {    # subgroup size -> (A2, D3, D4)
    2: (1.880, 0.000, 3.267),
    3: (1.023, 0.000, 2.574),
    4: (0.729, 0.000, 2.282),
    5: (0.577, 0.000, 2.114),
    6: (0.483, 0.000, 2.004),
    7: (0.419, 0.076, 1.924),
    8: (0.373, 0.136, 1.864),
    9: (0.337, 0.184, 1.816),
    10: (0.308, 0.223, 1.777),
}


XBAR_S_CONSTANTS: dict[int, tuple[float, float, float]] = {    # subgroup size -> (A3, B3, B4)
    2: (2.659, 0.000, 3.267),
    3: (1.954, 0.000, 2.568),
    4: (1.628, 0.000, 2.266),
    5: (1.427, 0.000, 2.089),
    6: (1.287, 0.030, 1.970),
    7: (1.182, 0.118, 1.882),
    8: (1.099, 0.185, 1.815),
    9: (1.032, 0.239, 1.761),
    10: (0.975, 0.284, 1.716),
}


def calculate_spec_limits(
    nominal_value: float,
    upper_tolerance: float,
    lower_tolerance: float,
) -> SpecLimits:
    return SpecLimits(
        usl=nominal_value + upper_tolerance,
        lsl=nominal_value + lower_tolerance,
    )


def check_spec_violation(actual_value: float, limits: SpecLimits) -> SpecViolation:
    if actual_value > limits.usl:
        return "above_usl"
    if actual_value < limits.lsl:
        return "below_lsl"
    return "within_spec"


def check_control_violation(
    actual_value: float,
    ucl: float,
    lcl: float,
) -> list[ControlViolation]:
    if actual_value > ucl:
        return ["above_ucl"]
    if actual_value < lcl:
        return ["below_lcl"]
    return []


def detect_control_limit_violations(
    values: Iterable[float],
    ucl: float,
    lcl: float,
) -> list[list[ControlViolation]]:
    """Apply only point-by-point control-limit checks.

    This is used by dispersion charts (MR/R/S), whose non-normal and dependent
    values should not reuse the zone/run rules intended for I/Xbar charts.
    """
    return [check_control_violation(value, ucl, lcl) for value in values]


def detect_control_rule_violations(
    values: Iterable[float],
    cl: float,
    ucl: float,
    lcl: float,
) -> list[list[str]]:
    ordered_values = list(values)
    sigma = _estimate_sigma_from_limits(cl, ucl, lcl)
    violations: list[list[str]] = [
        list(point_rules)
        for point_rules in detect_control_limit_violations(
            ordered_values,
            ucl,
            lcl,
        )
    ]

    for index, value in enumerate(ordered_values):
        if sigma is None:
            continue

        if index >= 2:
            window_indexes = range(index - 2, index + 1)
            above_indexes = [
                point_index
                for point_index in window_indexes
                if ordered_values[point_index] > cl + 2 * sigma
            ]
            below_indexes = [
                point_index
                for point_index in window_indexes
                if ordered_values[point_index] < cl - 2 * sigma
            ]
            if len(above_indexes) >= 2:
                _append_rule_to_points(
                    violations,
                    above_indexes,
                    "western_2_of_3_above_2sigma",
                )
            if len(below_indexes) >= 2:
                _append_rule_to_points(
                    violations,
                    below_indexes,
                    "western_2_of_3_below_2sigma",
                )

        if index >= 4:
            window_indexes = range(index - 4, index + 1)
            above_indexes = [
                point_index
                for point_index in window_indexes
                if ordered_values[point_index] > cl + sigma
            ]
            below_indexes = [
                point_index
                for point_index in window_indexes
                if ordered_values[point_index] < cl - sigma
            ]
            if len(above_indexes) >= 4:
                _append_rule_to_points(
                    violations,
                    above_indexes,
                    "western_4_of_5_above_1sigma",
                )
            if len(below_indexes) >= 4:
                _append_rule_to_points(
                    violations,
                    below_indexes,
                    "western_4_of_5_below_1sigma",
                )

        if index >= 7:
            window_indexes = range(index - 7, index + 1)
            window = [ordered_values[point_index] for point_index in window_indexes]
            if all(value > cl for value in window):
                _append_rule_to_points(
                    violations,
                    window_indexes,
                    "nelson_8_points_above_center",
                )
            if all(value < cl for value in window):
                _append_rule_to_points(
                    violations,
                    window_indexes,
                    "nelson_8_points_below_center",
                )

    return violations


def _append_rule_to_points(
    violations: list[list[str]],
    point_indexes: Iterable[int],
    rule: str,
) -> None:
    for point_index in point_indexes:
        if rule not in violations[point_index]:
            violations[point_index].append(rule)


def calculate_imr_limits(values: Iterable[float]) -> ImrLimits:
    ordered_values = list(values)
    sample_size = len(ordered_values)
    if sample_size < 3:
        raise ValueError("insufficient_data: at least 3 measurements are required")

    x_bar = mean(ordered_values)
    moving_ranges = [
        abs(current - previous)
        for previous, current in zip(ordered_values, ordered_values[1:])
    ]
    mr_bar = mean(moving_ranges)

    return ImrLimits(
        cl=x_bar,
        ucl=x_bar + 2.66 * mr_bar,
        lcl=x_bar - 2.66 * mr_bar,
        mr_cl=mr_bar,
        mr_ucl=3.267 * mr_bar,
        mr_lcl=0.0,
        sample_size=sample_size,
    )


def calculate_xbar_r_limits(subgroups: Iterable[Iterable[float]]) -> SubgroupLimits:
    normalized = _normalize_subgroups(subgroups)
    subgroup_size = len(normalized[0])
    if subgroup_size not in XBAR_R_CONSTANTS:
        raise ValueError("unsupported_subgroup_size: Xbar-R supports subgroup size 2-10")

    a2, d3, d4 = XBAR_R_CONSTANTS[subgroup_size]                                    # 根據子組大小獲取對應的常數
    subgroup_means = [mean(subgroup) for subgroup in normalized]
    subgroup_ranges = [max(subgroup) - min(subgroup) for subgroup in normalized]
    xbarbar = mean(subgroup_means)
    rbar = mean(subgroup_ranges)

    return SubgroupLimits(
        chart_type="Xbar-R",
        cl=xbarbar,
        ucl=xbarbar + a2 * rbar,
        lcl=xbarbar - a2 * rbar,
        secondary_chart_name="R",
        secondary_cl=rbar,
        secondary_ucl=d4 * rbar,
        secondary_lcl=d3 * rbar,
        sample_size=len(normalized),
        subgroup_size=subgroup_size,
    )


def calculate_xbar_s_limits(subgroups: Iterable[Iterable[float]]) -> SubgroupLimits:
    normalized = _normalize_subgroups(subgroups)
    subgroup_size = len(normalized[0])
    if subgroup_size not in XBAR_S_CONSTANTS:
        raise ValueError("unsupported_subgroup_size: Xbar-S supports subgroup size 2-10")

    a3, b3, b4 = XBAR_S_CONSTANTS[subgroup_size]
    subgroup_means = [mean(subgroup) for subgroup in normalized]
    subgroup_sigmas = [
        _sample_standard_deviation(subgroup, mean(subgroup)) for subgroup in normalized
    ]
    xbarbar = mean(subgroup_means)
    sbar = mean(subgroup_sigmas)

    return SubgroupLimits(
        chart_type="Xbar-S",
        cl=xbarbar,
        ucl=xbarbar + a3 * sbar,
        lcl=xbarbar - a3 * sbar,
        secondary_chart_name="S",
        secondary_cl=sbar,
        secondary_ucl=b4 * sbar,
        secondary_lcl=b3 * sbar,
        sample_size=len(normalized),
        subgroup_size=subgroup_size,
    )


def calculate_cp_cpk(
    values: Iterable[float],
    spec_limits: SpecLimits,
    target_value: float | None = None,
    long_term_sigma: float | None = None,
) -> Capability:
    ordered_values = list(values)
    sample_size = len(ordered_values)
    if sample_size < 2:
        return Capability(
            sample_size=sample_size,
            mean=None,
            sigma=None,
            usl=spec_limits.usl,
            lsl=spec_limits.lsl,
            cp=None,
            cpk=None,
            cpm=None,
            cpmk=None,
            ppk=None,
        )

    x_bar = mean(ordered_values)
    sigma = _sample_standard_deviation(ordered_values, x_bar)
    target = target_value if target_value is not None else (spec_limits.usl + spec_limits.lsl) / 2
    if sigma == 0:
        cp = None
        cpk = None
        cpm = None
        cpmk = None
    else:
        cp = (spec_limits.usl - spec_limits.lsl) / (6 * sigma)
        cpk = min(
            (spec_limits.usl - x_bar) / (3 * sigma),
            (x_bar - spec_limits.lsl) / (3 * sigma),
        )
        target_adjusted_sigma = sqrt(sigma**2 + (x_bar - target) ** 2)
        cpm = (spec_limits.usl - spec_limits.lsl) / (6 * target_adjusted_sigma)
        cpmk = min(spec_limits.usl - x_bar, x_bar - spec_limits.lsl) / (
            3 * target_adjusted_sigma
        )

    ppk_sigma = long_term_sigma if long_term_sigma is not None else sigma
    if ppk_sigma and ppk_sigma > 0:
        ppk = min(
            (spec_limits.usl - x_bar) / (3 * ppk_sigma),
            (x_bar - spec_limits.lsl) / (3 * ppk_sigma),
        )
    else:
        ppk = None

    return Capability(
        sample_size=sample_size,
        mean=x_bar,
        sigma=sigma,
        usl=spec_limits.usl,
        lsl=spec_limits.lsl,
        cp=cp,
        cpk=cpk,
        cpm=cpm,
        cpmk=cpmk,
        ppk=ppk,
    )


def build_trigger(
    is_out_of_spec: bool,
    is_out_of_control: bool | None,
) -> TriggerDecision:
    out_of_control = is_out_of_control is True

    if is_out_of_spec and out_of_control:
        severity: Severity = "high"
        event_type: EventType = "out_of_spec_and_out_of_control"
    elif is_out_of_spec:
        severity = "high"
        event_type = "out_of_spec"
    elif out_of_control:
        severity = "medium"
        event_type = "out_of_control"
    else:
        severity = "normal"
        event_type = "normal"

    abnormal = event_type != "normal"
    return TriggerDecision(                                 # 只要事件類型不是 normal 就視為異常，觸發後續流程
        should_create_abnormal_event=abnormal,
        should_alert=abnormal,
        should_call_ai_summary=abnormal,
        should_call_rag=abnormal,
        severity=severity,                                  # 嚴重程度根據事件類型設定，規格違反通常視為高嚴重性，控制違反視為中等嚴重性，兩者兼有則為最高嚴重性
        event_type=event_type,                              # 事件類型用於後續分析和報告，區分是規格違反、控制違反還是兩者兼有
    )


def _sample_standard_deviation(values: list[float], x_bar: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((value - x_bar) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance)


def _estimate_sigma_from_limits(cl: float, ucl: float, lcl: float) -> float | None:
    upper_sigma = (ucl - cl) / 3 if ucl > cl else None
    lower_sigma = (cl - lcl) / 3 if cl > lcl else None
    candidates = [value for value in (upper_sigma, lower_sigma) if value and value > 0]
    if not candidates:
        return None
    return mean(candidates)


def _normalize_subgroups(subgroups: Iterable[Iterable[float]]) -> list[list[float]]:
    normalized = [list(subgroup) for subgroup in subgroups]
    if len(normalized) < 2:
        raise ValueError("insufficient_data: at least 2 subgroups are required")
    subgroup_size = len(normalized[0])
    if subgroup_size < 2:
        raise ValueError("insufficient_data: each subgroup requires at least 2 values")
    if any(len(subgroup) != subgroup_size for subgroup in normalized):
        raise ValueError("invalid_subgroups: all subgroups must have the same size")
    return normalized
