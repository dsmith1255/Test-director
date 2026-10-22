"""Deterministic WIMNA projection models.

All power values are watts, energy is joules, distance is metres, and time is
seconds.  Positive battery power DISCHARGES the battery; negative power
recovers energy.  Temperatures here are custom model state, never hidden AC
component temperatures.  Callers must retain the associated parameter records
and must not present projections as a measured physical effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import ceil, exp, isfinite
from statistics import fmean
from typing import Mapping, Sequence


EVIDENCE = frozenset({"CASE", "MEASURED", "PHYSICS_DERIVED", "ASSUMED", "SYNTHETIC"})


def _finite(value: float, name: str) -> float:
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _nonnegative(value: float, name: str) -> float:
    _finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


@dataclass(frozen=True)
class ModelParameter:
    """A reportable value; null remains unavailable rather than becoming zero."""

    value: float | None
    unit: str
    source: str
    applicable_version: str
    evidence: str
    low: float | None = None
    high: float | None = None

    def __post_init__(self) -> None:
        if self.evidence not in EVIDENCE:
            raise ValueError(f"unknown evidence category: {self.evidence}")
        if not self.unit or not self.source or not self.applicable_version:
            raise ValueError("unit, source and applicable_version are required")
        for name in ("value", "low", "high"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, name)
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("low exceeds high")
        if self.value is not None:
            if self.low is not None and self.value < self.low:
                raise ValueError("value below range")
            if self.high is not None and self.value > self.high:
                raise ValueError("value above range")

    def require(self) -> float:
        if self.value is None:
            raise ValueError(f"parameter unavailable: {self.source} ({self.unit})")
        return self.value


def derived_evidence(*groups: Sequence[str]) -> tuple[str, ...]:
    """A derived value keeps every input provenance category."""
    categories = {item for group in groups for item in group}
    if categories - EVIDENCE:
        raise ValueError(f"unknown evidence categories: {categories - EVIDENCE}")
    if categories:
        categories.add("PHYSICS_DERIVED")
    return tuple(sorted(categories))


@dataclass(frozen=True)
class CurveResult:
    value: float
    extrapolated: bool = False
    clamped: bool = False
    warnings: tuple[str, ...] = ()


def curve_value(points: Sequence[tuple[float, float]], x: float, *, extrapolate: bool = False) -> CurveResult:
    """Piecewise linear interpolation; out-of-domain use is always disclosed."""
    _finite(x, "x")
    if len(points) < 2:
        raise ValueError("curve needs at least two points")
    for i, (px, py) in enumerate(points):
        _finite(px, "curve x")
        _finite(py, "curve y")
        if i and px <= points[i - 1][0]:
            raise ValueError("curve x values must increase strictly")
    if x < points[0][0]:
        if not extrapolate:
            return CurveResult(points[0][1], clamped=True, warnings=("below curve domain; clamped",))
        segment = (points[0], points[1])
        warning = "below curve domain; linear extrapolation"
    elif x > points[-1][0]:
        if not extrapolate:
            return CurveResult(points[-1][1], clamped=True, warnings=("above curve domain; clamped",))
        segment = (points[-2], points[-1])
        warning = "above curve domain; linear extrapolation"
    else:
        if x == points[-1][0]:
            return CurveResult(points[-1][1])
        segment = next((points[i], points[i + 1]) for i in range(len(points) - 1) if points[i][0] <= x <= points[i + 1][0])
        warning = ""
    (x0, y0), (x1, y1) = segment
    y = y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return CurveResult(y, extrapolated=bool(warning), warnings=(warning,) if warning else ())


@dataclass(frozen=True)
class TireState:
    wear_fraction: float
    temperature_c: float
    accumulated_work_m: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.wear_fraction <= 1:
            raise ValueError("wear_fraction must be within 0..1")
        _finite(self.temperature_c, "tire temperature")
        _nonnegative(self.accumulated_work_m, "accumulated tire work")


@dataclass(frozen=True)
class TireSample:
    dt_s: float
    speed_m_s: float
    load_ratio: float
    slip_ratio: float
    air_temp_c: float
    track_temp_c: float

    def __post_init__(self) -> None:
        if self.dt_s <= 0 or not isfinite(self.dt_s):
            raise ValueError("tire dt_s must be positive")
        _nonnegative(self.speed_m_s, "speed")
        _nonnegative(self.load_ratio, "load ratio")
        for name in ("slip_ratio", "air_temp_c", "track_temp_c"):
            _finite(getattr(self, name), name)


@dataclass(frozen=True)
class TireConfig:
    """Local workload proxy; coefficients require separate parameter records."""

    wear_per_work_m: float
    heating_c_per_work_m: float
    cooling_per_s: float
    slip_gain: float
    track_coupling_fraction: float
    wear_pace_curve: tuple[tuple[float, float], ...] | None = None
    temperature_pace_curve: tuple[tuple[float, float], ...] | None = None
    parameter_records: Mapping[str, ModelParameter] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("wear_per_work_m", "heating_c_per_work_m", "cooling_per_s", "slip_gain"):
            _nonnegative(getattr(self, name), name)
        if not 0 <= self.track_coupling_fraction <= 1:
            raise ValueError("track_coupling_fraction must be 0..1")


@dataclass(frozen=True)
class TireStep:
    state: TireState
    workload_m: float
    warnings: tuple[str, ...] = ()


def project_tire_step(state: TireState, sample: TireSample, config: TireConfig) -> TireStep:
    work_m = sample.speed_m_s * sample.dt_s * sample.load_ratio * (1 + config.slip_gain * abs(sample.slip_ratio))
    wear = min(1.0, state.wear_fraction + work_m * config.wear_per_work_m)
    equilibrium_c = sample.air_temp_c + config.track_coupling_fraction * (sample.track_temp_c - sample.air_temp_c)
    heat_rate_c_s = config.heating_c_per_work_m * work_m / sample.dt_s
    k = config.cooling_per_s
    if k:
        decay = exp(-k * sample.dt_s)
        temperature_c = equilibrium_c + (state.temperature_c - equilibrium_c) * decay + heat_rate_c_s * (1 - decay) / k
    else:
        temperature_c = state.temperature_c + heat_rate_c_s * sample.dt_s
    warnings = ("tire wear reached model boundary",) if wear == 1 and state.wear_fraction + work_m * config.wear_per_work_m > 1 else ()
    return TireStep(TireState(wear, temperature_c, state.accumulated_work_m + work_m), work_m, warnings)


def project_tires(
    states: Mapping[str, TireState], samples: Mapping[str, TireSample], config: TireConfig
) -> dict[str, TireStep]:
    if states.keys() != samples.keys():
        raise ValueError("every tire requires its own state and sample")
    return {wheel: project_tire_step(state, samples[wheel], config) for wheel, state in states.items()}


def tire_pace_penalty_s(state: TireState, config: TireConfig) -> CurveResult | None:
    if config.wear_pace_curve is None or config.temperature_pace_curve is None:
        return None
    wear = curve_value(config.wear_pace_curve, state.wear_fraction)
    temperature = curve_value(config.temperature_pace_curve, state.temperature_c)
    return CurveResult(wear.value + temperature.value, clamped=wear.clamped or temperature.clamped, warnings=wear.warnings + temperature.warnings)


def fuel_mass_kg(volume_l: float, density_kg_per_l: float | None) -> float | None:
    _nonnegative(volume_l, "fuel volume")
    if density_kg_per_l is None:
        return None
    if density_kg_per_l <= 0 or not isfinite(density_kg_per_l):
        raise ValueError("fuel density must be positive")
    return volume_l * density_kg_per_l


def fuel_consumption(start: float, end: float, lap_count: int) -> float:
    """Return consumption per lap in the same unit as the source channel."""
    _nonnegative(start, "starting fuel")
    _nonnegative(end, "ending fuel")
    if lap_count <= 0 or end > start:
        raise ValueError("invalid fuel interval")
    return (start - end) / lap_count


@dataclass(frozen=True)
class FuelContrast:
    seconds_per_litre: float | None
    seconds_per_kg: float | None
    reference_laps: int
    contrast_laps: int
    exploratory: bool
    warnings: tuple[str, ...]


def fuel_local_contrast(
    reference_lap_times_s: Sequence[float], contrast_lap_times_s: Sequence[float],
    reference_fuel_l: float, contrast_fuel_l: float, *, density_kg_per_l: float | None,
    tire_matched: bool, battery_matched: bool,
) -> FuelContrast:
    """A local exploratory slope, valid only when tire and battery states match."""
    for value in (*reference_lap_times_s, *contrast_lap_times_s):
        if value <= 0 or not isfinite(value):
            raise ValueError("lap times must be positive")
    _nonnegative(reference_fuel_l, "reference fuel")
    _nonnegative(contrast_fuel_l, "contrast fuel")
    n_ref, n_con = len(reference_lap_times_s), len(contrast_lap_times_s)
    warnings: list[str] = ["local exploratory contrast; no universal fuel slope or precision interval"]
    if n_ref < 1 or n_con < 1 or reference_fuel_l == contrast_fuel_l:
        warnings.append("insufficient contrast")
        return FuelContrast(None, None, n_ref, n_con, True, tuple(warnings))
    if not tire_matched or not battery_matched:
        warnings.append("tire or battery state unmatched; slope unavailable")
        return FuelContrast(None, None, n_ref, n_con, True, tuple(warnings))
    if density_kg_per_l is not None:
        fuel_mass_kg(1.0, density_kg_per_l)
    slope_l = (fmean(contrast_lap_times_s) - fmean(reference_lap_times_s)) / (contrast_fuel_l - reference_fuel_l)
    slope_kg = None if density_kg_per_l is None else slope_l / density_kg_per_l
    if density_kg_per_l is None:
        warnings.append("fuel density unavailable; mass sensitivity unavailable")
    return FuelContrast(slope_l, slope_kg, n_ref, n_con, True, tuple(warnings))


@dataclass(frozen=True)
class EnergySample:
    dt_s: float
    battery_power_w: float
    requested_power_w: float | None = None
    delivered_power_w: float | None = None

    def __post_init__(self) -> None:
        if self.dt_s <= 0 or not isfinite(self.dt_s):
            raise ValueError("energy dt_s must be positive")
        _finite(self.battery_power_w, "battery power")
        for value in (self.requested_power_w, self.delivered_power_w):
            if value is not None:
                _finite(value, "power")


@dataclass(frozen=True)
class EnergyResult:
    starting_j: float
    ending_j: float
    deployed_j: float
    recovered_j: float
    net_discharge_j: float
    requested_j: float | None
    delivered_j: float | None
    warnings: tuple[str, ...]


def signed_battery_power(source_power_w: float, *, positive_means: str) -> float:
    """Normalize a declared source convention to positive discharge."""
    _finite(source_power_w, "source power")
    if positive_means == "discharge":
        return source_power_w
    if positive_means == "recovery":
        return -source_power_w
    raise ValueError("positive_means must be discharge or recovery")


def integrate_energy(
    samples: Sequence[EnergySample], starting_j: float, *, usable_capacity_j: float | None = None,
    deployment_allowance_j: float | None = None, recovery_allowance_j: float | None = None,
) -> EnergyResult:
    _nonnegative(starting_j, "starting energy")
    if usable_capacity_j is not None and (usable_capacity_j <= 0 or not isfinite(usable_capacity_j)):
        raise ValueError("usable capacity must be positive")
    for name, allowance in (("deployment allowance", deployment_allowance_j),
                            ("recovery allowance", recovery_allowance_j)):
        if allowance is not None:
            _nonnegative(allowance, name)
    deployed = sum(max(0.0, s.battery_power_w) * s.dt_s for s in samples)
    recovered = sum(max(0.0, -s.battery_power_w) * s.dt_s for s in samples)
    end = starting_j - deployed + recovered
    requested = sum(s.requested_power_w * s.dt_s for s in samples) if samples and all(s.requested_power_w is not None for s in samples) else None
    delivered = sum(s.delivered_power_w * s.dt_s for s in samples) if samples and all(s.delivered_power_w is not None for s in samples) else None
    warnings: list[str] = []
    if end < -1e-9:
        warnings.append("energy projected below zero; policy infeasible")
    if usable_capacity_j is not None and end > usable_capacity_j + 1e-9:
        warnings.append("energy projected above usable capacity; policy infeasible")
    if usable_capacity_j is not None and starting_j > usable_capacity_j + 1e-9:
        warnings.append("starting energy exceeds usable capacity")
    if deployment_allowance_j is not None and deployed > deployment_allowance_j + 1e-9:
        warnings.append("deployment exceeds declared allowance")
    if recovery_allowance_j is not None and recovered > recovery_allowance_j + 1e-9:
        warnings.append("recovery exceeds declared allowance")
    return EnergyResult(starting_j, end, deployed, recovered, deployed - recovered, requested, delivered, tuple(warnings))


@dataclass(frozen=True)
class RepaymentResult:
    remaining_debt_j: float
    projected_recovery_laps: float | None
    projected_time_cost_s: float | None
    warning: str | None


def project_energy_repayment(debt_j: float, *, net_recovery_j_per_lap: float | None, cost_s_per_recovery_lap: float | None) -> RepaymentResult:
    _nonnegative(debt_j, "energy debt")
    if debt_j == 0:
        return RepaymentResult(0, 0, 0, None)
    if net_recovery_j_per_lap is None or cost_s_per_recovery_lap is None:
        return RepaymentResult(debt_j, None, None, "recovery rate or time cost unavailable")
    if net_recovery_j_per_lap <= 0 or cost_s_per_recovery_lap < 0:
        return RepaymentResult(debt_j, None, None, "recovery policy cannot repay debt")
    laps = debt_j / net_recovery_j_per_lap
    return RepaymentResult(debt_j, laps, laps * cost_s_per_recovery_lap, None)


@dataclass(frozen=True)
class ThermalNode:
    """A custom lumped node.  Heat exchange is symmetric between named nodes."""

    heat_capacity_j_per_c: float
    heat_w_at_full_load: float
    cooling_w_per_c: float
    speed_cooling_gain_per_mps: float
    connections_w_per_c: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.heat_capacity_j_per_c <= 0 or not isfinite(self.heat_capacity_j_per_c):
            raise ValueError("heat capacity must be positive")
        for name in ("heat_w_at_full_load", "cooling_w_per_c", "speed_cooling_gain_per_mps"):
            _nonnegative(getattr(self, name), name)
        for conductance in self.connections_w_per_c.values():
            _nonnegative(conductance, "node conductance")


@dataclass(frozen=True)
class ThermalController:
    warning_c: float
    derate_c: float
    recovery_c: float
    maximum_reduction: float
    ramp_fraction_per_s: float
    timeout_s: float

    def __post_init__(self) -> None:
        if not self.warning_c < self.derate_c or not self.recovery_c < self.derate_c:
            raise ValueError("warning and recovery thresholds must be below derate")
        if not 0 <= self.maximum_reduction <= 1:
            raise ValueError("maximum reduction must be within 0..1")
        _nonnegative(self.ramp_fraction_per_s, "derate ramp")
        if self.timeout_s <= 0 or not isfinite(self.timeout_s):
            raise ValueError("timeout must be positive")


@dataclass(frozen=True)
class ThermalConfig:
    nodes: Mapping[str, ThermalNode]
    controller_node: str
    controller: ThermalController
    parameter_records: Mapping[str, ModelParameter] = field(default_factory=dict)
    label: str = "custom lumped thermal model"

    def __post_init__(self) -> None:
        if self.controller_node not in self.nodes or not self.nodes:
            raise ValueError("controller node missing")
        for name, node in self.nodes.items():
            for other, conductance in node.connections_w_per_c.items():
                if other not in self.nodes or other == name:
                    raise ValueError("invalid thermal node connection")
                if self.nodes[other].connections_w_per_c.get(name) != conductance:
                    raise ValueError("thermal node connections must be symmetric")


@dataclass(frozen=True)
class ThermalState:
    temperatures_c: Mapping[str, float]
    reduction_fraction: float = 0.0
    controller_active: bool = False
    active_s: float = 0.0
    timed_out: bool = False

    def __post_init__(self) -> None:
        for temperature in self.temperatures_c.values():
            _finite(temperature, "modeled temperature")
        if not 0 <= self.reduction_fraction <= 1:
            raise ValueError("reduction fraction must be 0..1")
        _nonnegative(self.active_s, "active time")


@dataclass(frozen=True)
class ThermalSample:
    dt_s: float
    workload_fraction: float
    speed_m_s: float
    ambient_c: float
    airflow_factor: float = 1.0
    track_temp_c: float | None = None
    actuation_supported: bool = False
    observed_reduction_fraction: float | None = None
    effect_verified: bool = False
    battery_limit_excluded: bool = False
    rule_limit_excluded: bool = False
    baseline_comparable: bool = False
    effect_tolerance_fraction: float = 0.05

    def __post_init__(self) -> None:
        if self.dt_s <= 0 or not isfinite(self.dt_s):
            raise ValueError("thermal dt_s must be positive")
        _nonnegative(self.workload_fraction, "thermal workload")
        _nonnegative(self.speed_m_s, "speed")
        _finite(self.ambient_c, "air temperature")
        _nonnegative(self.airflow_factor, "airflow factor")
        if self.track_temp_c is not None:
            _finite(self.track_temp_c, "track temperature")
        if self.observed_reduction_fraction is not None and not 0 <= self.observed_reduction_fraction <= 1:
            raise ValueError("observed reduction must be within 0..1")
        _nonnegative(self.effect_tolerance_fraction, "effect tolerance")
        if self.effect_verified and not (self.actuation_supported and self.observed_reduction_fraction is not None
                                         and self.battery_limit_excluded and self.rule_limit_excluded
                                         and self.baseline_comparable):
            raise ValueError("verified effect needs observation and battery/rule/comparability checks")


@dataclass(frozen=True)
class ThermalStep:
    state: ThermalState
    warning: bool
    derate_threshold_exceeded: bool
    requested_reduction_fraction: float
    physical_effect_status: str
    generated_j: float
    ambient_loss_j: float
    peak_temperatures_c: Mapping[str, float]
    time_above_warning_s: float
    time_above_derate_s: float
    derating_duration_s: float
    warnings: tuple[str, ...]


def _thermal_substep(state: ThermalState, sample: ThermalSample, config: ThermalConfig, dt_s: float) -> tuple[ThermalState, float, float]:
    old = state.temperatures_c
    generated_j = 0.0
    ambient_loss_j = 0.0
    new: dict[str, float] = {}
    for name, node in config.nodes.items():
        heat_w = node.heat_w_at_full_load * sample.workload_fraction
        cool_w = (node.cooling_w_per_c * (1 + node.speed_cooling_gain_per_mps * sample.speed_m_s)
                  * sample.airflow_factor * (old[name] - sample.ambient_c))
        exchange_w = sum(g * (old[other] - old[name]) for other, g in node.connections_w_per_c.items())
        new[name] = old[name] + dt_s * (heat_w - cool_w + exchange_w) / node.heat_capacity_j_per_c
        generated_j += heat_w * dt_s
        ambient_loss_j += cool_w * dt_s
    control = config.controller
    controlled_temp = new[config.controller_node]
    active = state.controller_active
    timed_out = state.timed_out
    active_s = state.active_s
    if controlled_temp <= control.recovery_c:
        active = False
        timed_out = False
        active_s = 0.0
    elif controlled_temp >= control.derate_c and not timed_out:
        active = True
    if active:
        active_s += dt_s
        if active_s >= control.timeout_s:
            active = False
            timed_out = True
    target = control.maximum_reduction if active else 0.0
    step = control.ramp_fraction_per_s * dt_s
    reduction = min(target, state.reduction_fraction + step) if target > state.reduction_fraction else max(target, state.reduction_fraction - step)
    return ThermalState(new, reduction, active, active_s, timed_out), generated_j, ambient_loss_j


def step_thermal_system(state: ThermalState, sample: ThermalSample, config: ThermalConfig) -> ThermalStep:
    """Advance custom heat balances; controls are only requests until effect is verified."""
    if set(state.temperatures_c) != set(config.nodes):
        raise ValueError("thermal state does not match configured nodes")
    time_constants = []
    for node in config.nodes.values():
        conductance = (node.cooling_w_per_c * (1 + node.speed_cooling_gain_per_mps * sample.speed_m_s)
                       * sample.airflow_factor + sum(node.connections_w_per_c.values()))
        if conductance:
            time_constants.append(node.heat_capacity_j_per_c / conductance)
    max_dt = min(0.25, 0.1 * min(time_constants)) if time_constants else 0.25
    steps = ceil(sample.dt_s / max_dt)
    if steps > 100000:
        raise ValueError("thermal step too stiff for bounded deterministic integration")
    dt = sample.dt_s / steps
    generated = lost = time_warning = time_derate = derate_duration = 0.0
    current = state
    peaks = dict(state.temperatures_c)
    for _ in range(steps):
        current, heat_j, cool_j = _thermal_substep(current, sample, config, dt)
        generated += heat_j
        lost += cool_j
        for name, temp in current.temperatures_c.items():
            peaks[name] = max(peaks[name], temp)
        temp = current.temperatures_c[config.controller_node]
        if temp >= config.controller.warning_c:
            time_warning += dt
        if temp >= config.controller.derate_c:
            time_derate += dt
        if current.reduction_fraction > 0:
            derate_duration += dt
    temperature = current.temperatures_c[config.controller_node]
    observed_matches = (sample.observed_reduction_fraction is not None and current.reduction_fraction > 0
                        and abs(sample.observed_reduction_fraction - current.reduction_fraction)
                        <= sample.effect_tolerance_fraction)
    if sample.effect_verified and observed_matches:
        status = "verified"
    elif sample.actuation_supported:
        status = "unverified"
    else:
        status = "analysis_only"
    warnings: list[str] = []
    if sample.effect_verified and not observed_matches:
        warnings.append("observed reduction did not match requested intervention")
    if current.timed_out:
        warnings.append("thermal intervention timed out; latched until recovery threshold")
    if current.reduction_fraction > 0 and status != "verified":
        warnings.append("requested derating has no verified physical effect")
    return ThermalStep(
        current, temperature >= config.controller.warning_c,
        temperature >= config.controller.derate_c, current.reduction_fraction,
        status, generated, lost, peaks, time_warning, time_derate,
        derate_duration, tuple(warnings),
    )


@dataclass(frozen=True)
class ThermalScenario:
    name: str
    track_temp_c: float
    ambient_c: float | None
    airflow_factor: float = 1.0
    evidence: tuple[str, ...] = ("ASSUMED",)


def case_scenario_presets(ambient_c: float | None = None) -> tuple[ThermalScenario, ...]:
    """Proposed case anchors; ambient is independent and must be supplied."""
    return (
        ThermalScenario("reference_R4", 29.0, ambient_c, evidence=("CASE", "ASSUMED")),
        ThermalScenario("elevated_R8", 33.0, ambient_c, evidence=("CASE", "ASSUMED")),
        ThermalScenario("severe_R5", 36.0, ambient_c, evidence=("CASE", "ASSUMED")),
    )


@dataclass(frozen=True)
class ThermalScenarioResult:
    name: str
    available: bool
    peak_temperatures_c: Mapping[str, float] | None
    time_above_warning_s: float | None
    time_above_derate_s: float | None
    derating_duration_s: float | None
    ending_state: ThermalState | None
    calculation_mode: str
    physical_effect_status: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


def run_thermal_scenario(
    trace: Sequence[ThermalSample], config: ThermalConfig, scenario: ThermalScenario,
    *, initial_state: ThermalState | None = None, repeats: int = 1,
    observed_track_range_c: tuple[float, float] = (24.0, 36.0),
) -> ThermalScenarioResult:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    range_low, range_high = observed_track_range_c
    if not (isfinite(range_low) and isfinite(range_high) and range_low <= range_high):
        raise ValueError("invalid observed track-temperature range")
    if scenario.ambient_c is None:
        return ThermalScenarioResult(scenario.name, False, None, None, None, None, None,
                                     "scenario_projection", "analysis_only", scenario.evidence,
                                     ("ambient air temperature required; track temperature is not cooling air",))
    if not trace:
        return ThermalScenarioResult(scenario.name, False, None, None, None, None, None,
                                     "scenario_projection", "analysis_only", scenario.evidence,
                                     ("workload trace unavailable",))
    _finite(scenario.ambient_c, "scenario ambient")
    _nonnegative(scenario.airflow_factor, "scenario airflow")
    state = initial_state or ThermalState({name: scenario.ambient_c for name in config.nodes})
    peaks = dict(state.temperatures_c)
    time_warning = time_derate = duration = 0.0
    for _ in range(repeats):
        for original in trace:
            sample = replace(original, ambient_c=scenario.ambient_c,
                             track_temp_c=scenario.track_temp_c,
                             airflow_factor=original.airflow_factor * scenario.airflow_factor,
                             actuation_supported=False, effect_verified=False,
                             observed_reduction_fraction=None,
                             battery_limit_excluded=False, rule_limit_excluded=False,
                             baseline_comparable=False)
            result = step_thermal_system(state, sample, config)
            state = result.state
            for name, temp in result.peak_temperatures_c.items():
                peaks[name] = max(peaks[name], temp)
            time_warning += result.time_above_warning_s
            time_derate += result.time_above_derate_s
            duration += result.derating_duration_s
    warnings = ("modeled component state and restrictions; no native physical effect verified",)
    if not range_low <= scenario.track_temp_c <= range_high:
        warnings += (f"track temperature outside observed case range {range_low:g}..{range_high:g} C",)
    return ThermalScenarioResult(scenario.name, True, peaks, time_warning, time_derate,
                                 duration, state, "scenario_projection", "analysis_only",
                                 derived_evidence(scenario.evidence, ("ASSUMED",)), warnings)


def warm_start_state(config: ThermalConfig, ambient_c: float, *, margin_below_derate_c: float = 1.0) -> ThermalState:
    """Initialize only the custom model near derating; no native state is touched."""
    _finite(ambient_c, "ambient air")
    _nonnegative(margin_below_derate_c, "warm-start margin")
    temperatures = {name: ambient_c for name in config.nodes}
    temperatures[config.controller_node] = config.controller.derate_c - margin_below_derate_c
    return ThermalState(temperatures)


def batch_thermal_scenarios(
    trace: Sequence[ThermalSample], profiles: Mapping[str, ThermalConfig],
    scenarios: Sequence[ThermalScenario], *, repeats: int = 1,
    observed_track_range_c: tuple[float, float] = (24.0, 36.0),
) -> dict[str, dict[str, ThermalScenarioResult]]:
    """Deterministic profile by environment sweep; no race win is encoded."""
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("scenario names must be unique")
    return {
        profile_name: {
            scenario.name: run_thermal_scenario(
                trace, config, scenario, repeats=repeats,
                observed_track_range_c=observed_track_range_c,
            )
            for scenario in scenarios
        }
        for profile_name, config in profiles.items()
    }


def thermal_assumption_profiles() -> dict[str, ThermalConfig]:
    """Usable engineering assumptions; none is an identified FH26 failure limit."""
    profiles: dict[str, ThermalConfig] = {}
    # Low/base/high denote thermal stress, not confidence quantiles.
    for name, heat_w, electrical_cooling, cooler_cooling, threshold in (
        ("low", 7000.0, 200.0, 250.0, 92.0),
        ("base", 10000.0, 150.0, 190.0, 85.0),
        ("high", 13000.0, 100.0, 140.0, 78.0),
    ):
        nodes = {
            "electrical_lumped": ThermalNode(120000.0, heat_w, electrical_cooling, 0.012, {"cooling_lumped": 100.0}),
            "cooling_lumped": ThermalNode(180000.0, 0.0, cooler_cooling, 0.020, {"electrical_lumped": 100.0}),
        }
        controller = ThermalController(threshold - 10.0, threshold, threshold - 5.0, 0.25, 0.01, 90.0)
        values = {
            "electrical.heat_capacity": (120000.0, "J/C"),
            "electrical.heat_full_load": (heat_w, "W"),
            "electrical.cooling": (electrical_cooling, "W/C"),
            "electrical.speed_gain": (0.012, "s/m"),
            "cooling.heat_capacity": (180000.0, "J/C"),
            "cooling.heat_full_load": (0.0, "W"),
            "cooling.cooling": (cooler_cooling, "W/C"),
            "cooling.speed_gain": (0.020, "s/m"),
            "node.coupling": (100.0, "W/C"),
            "control.warning": (controller.warning_c, "C"),
            "control.derate": (controller.derate_c, "C"),
            "control.recovery": (controller.recovery_c, "C"),
            "control.maximum_reduction": (0.25, "fraction"),
            "control.ramp": (0.01, "fraction/s"),
            "control.timeout": (90.0, "s"),
        }
        ranges = {
            "electrical.heat_full_load": (7000.0, 13000.0),
            "electrical.cooling": (100.0, 200.0),
            "cooling.cooling": (140.0, 250.0),
            "control.warning": (68.0, 82.0),
            "control.derate": (78.0, 92.0),
            "control.recovery": (73.0, 87.0),
        }
        records = {
            key: ModelParameter(value, unit,
                                "WIMNA engineering assumption; not identified from case outcomes",
                                "FH26 unverified", "ASSUMED",
                                *(ranges.get(key, (value, value))))
            for key, (value, unit) in values.items()
        }
        profiles[name] = ThermalConfig(nodes, "electrical_lumped", controller, records,
                                       f"{name} thermal stress, two custom lumped nodes")
    return profiles


@dataclass(frozen=True)
class StintConfig:
    """One repeated reference pattern; base pace is at the supplied initial state.

    Every pace correction is a *difference* from that initial state, avoiding a
    second charge for effects already present in the measured base lap.  Missing
    correction parameters make total time unavailable rather than silently zero.
    Restriction response coefficients are assumptions to be recorded by caller.
    """

    lap_count: int
    base_lap_time_s: float | None
    starting_fuel_l: float | None
    fuel_burn_l_per_lap: float | None
    fuel_density_kg_per_l: float | None
    fuel_seconds_per_kg: float | None
    initial_tires: Mapping[str, TireState]
    tire_samples_per_lap: Mapping[str, Sequence[TireSample]]
    tire_model: TireConfig | None
    starting_battery_j: float | None
    usable_battery_j: float | None
    energy_samples_per_lap: Sequence[EnergySample]
    energy_pace_delta_s_per_lap: float | None
    thermal_model: ThermalConfig | None
    thermal_samples_per_lap: Sequence[ThermalSample]
    thermal_initial_state: ThermalState | None = None
    derate_seconds_at_full_reduction: float | None = None
    restriction_speed_response: float | None = None
    restriction_workload_response: float | None = None
    restriction_energy_response: float | None = None
    evidence: tuple[str, ...] = ("ASSUMED",)

    def __post_init__(self) -> None:
        if self.lap_count <= 0:
            raise ValueError("lap_count must be positive")
        for name in ("base_lap_time_s", "fuel_burn_l_per_lap", "energy_pace_delta_s_per_lap",
                     "derate_seconds_at_full_reduction", "restriction_speed_response",
                     "restriction_workload_response", "restriction_energy_response",
                     "fuel_seconds_per_kg"):
            value = getattr(self, name)
            if value is not None:
                _finite(value, name)
        if self.base_lap_time_s is not None and self.base_lap_time_s <= 0:
            raise ValueError("base lap time must be positive")
        if self.starting_fuel_l is not None:
            _nonnegative(self.starting_fuel_l, "starting fuel")
        if self.fuel_burn_l_per_lap is not None:
            _nonnegative(self.fuel_burn_l_per_lap, "fuel burn")
        if self.starting_battery_j is not None:
            _nonnegative(self.starting_battery_j, "starting battery")
        if self.usable_battery_j is not None and (self.usable_battery_j <= 0 or not isfinite(self.usable_battery_j)):
            raise ValueError("usable battery energy must be positive")
        if self.fuel_density_kg_per_l is not None:
            fuel_mass_kg(1.0, self.fuel_density_kg_per_l)
        if self.derate_seconds_at_full_reduction is not None:
            _nonnegative(self.derate_seconds_at_full_reduction, "derating time cost")
        for name in ("restriction_speed_response", "restriction_workload_response",
                     "restriction_energy_response"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be within 0..1")
        if self.initial_tires.keys() != self.tire_samples_per_lap.keys():
            raise ValueError("each tire requires a lap trace")
        for item in self.evidence:
            if item not in EVIDENCE:
                raise ValueError("unknown evidence")


@dataclass(frozen=True)
class StintLap:
    number: int
    predicted_time_s: float | None
    starting_fuel_l: float | None
    ending_fuel_l: float | None
    starting_battery_j: float | None
    ending_battery_j: float | None
    ending_tires: Mapping[str, TireState]
    modeled_temperatures_c: Mapping[str, float] | None
    average_requested_reduction: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class StintResult:
    laps: tuple[StintLap, ...]
    total_time_s: float | None
    calculation_mode: str
    physical_effect_status: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


def _average_tire_penalty(states: Mapping[str, TireState], config: TireConfig) -> tuple[float | None, tuple[str, ...]]:
    penalties = [tire_pace_penalty_s(state, config) for state in states.values()]
    if not penalties or any(p is None for p in penalties):
        return None, ("tire pace curves unavailable",)
    valid = [p for p in penalties if p is not None]
    return fmean(p.value for p in valid), tuple(warning for p in valid for warning in p.warnings)


def project_stint(config: StintConfig) -> StintResult:
    """Project fuel, per-tire state, battery and modeled thermal consequences.

    The repeated trace is a local approximation.  Late tire cliffs outside
    known curve domains are clamped and warned about; callers should widen the
    uncertainty range instead of treating this as full-stint validation.
    """
    warnings: list[str] = ["repeated local pattern; longer-stint behavior is a projection"]
    fuel = config.starting_fuel_l
    battery = config.starting_battery_j
    tires = dict(config.initial_tires)
    thermal = config.thermal_initial_state
    if config.thermal_model is not None and thermal is None and config.thermal_samples_per_lap:
        ambient = config.thermal_samples_per_lap[0].ambient_c
        thermal = ThermalState({name: ambient for name in config.thermal_model.nodes})
    initial_penalty, initial_tire_warnings = ((None, ("tire model unavailable",)) if config.tire_model is None
                                               else _average_tire_penalty(tires, config.tire_model))
    warnings.extend(initial_tire_warnings)
    lap_results: list[StintLap] = []
    prior_reduction = 0.0
    for number in range(1, config.lap_count + 1):
        lap_warnings: list[str] = []
        fuel_start = fuel
        battery_start = battery
        # Thermal and speed are coupled through the preceding lap's observed
        # model restriction.  This one-lap-lag approximation is explicit.
        reduction = None
        if config.thermal_model is not None and thermal is not None and config.thermal_samples_per_lap:
            weighted_reduction = elapsed = 0.0
            for original in config.thermal_samples_per_lap:
                if prior_reduction > 0 and config.restriction_speed_response is None:
                    lap_warnings.append("restriction speed response unavailable")
                    speed_scale = 1.0
                else:
                    speed_scale = max(0.0, 1 - prior_reduction * (config.restriction_speed_response or 0.0))
                sample = replace(original, speed_m_s=original.speed_m_s * speed_scale,
                                 actuation_supported=False, effect_verified=False,
                                 observed_reduction_fraction=None,
                                 battery_limit_excluded=False, rule_limit_excluded=False,
                                 baseline_comparable=False)
                thermal_step = step_thermal_system(thermal, sample, config.thermal_model)
                thermal = thermal_step.state
                weighted_reduction += thermal_step.requested_reduction_fraction * sample.dt_s
                elapsed += sample.dt_s
            reduction = weighted_reduction / elapsed
        elif config.thermal_model is not None:
            lap_warnings.append("thermal trace or initial state unavailable")
        prior_reduction = reduction or 0.0

        if config.tire_model is not None and tires:
            updated: dict[str, TireState] = {}
            for wheel, state in tires.items():
                for original in config.tire_samples_per_lap[wheel]:
                    if reduction and (config.restriction_speed_response is None or config.restriction_workload_response is None):
                        lap_warnings.append("restricted tire workload response unavailable")
                        speed_scale = load_scale = 1.0
                    else:
                        speed_scale = max(0.0, 1 - (reduction or 0) * (config.restriction_speed_response or 0))
                        load_scale = max(0.0, 1 - (reduction or 0) * (config.restriction_workload_response or 0))
                    sample = replace(original, speed_m_s=original.speed_m_s * speed_scale,
                                     load_ratio=original.load_ratio * load_scale)
                    step = project_tire_step(state, sample, config.tire_model)
                    state = step.state
                    lap_warnings.extend(step.warnings)
                updated[wheel] = state
            tires = updated
        elif tires:
            lap_warnings.append("tire model unavailable")
        current_penalty, tire_warnings = ((None, ("tire model unavailable",)) if config.tire_model is None
                                          else _average_tire_penalty(tires, config.tire_model))
        lap_warnings.extend(tire_warnings)

        if battery is not None and config.energy_samples_per_lap:
            energy_samples = []
            for original in config.energy_samples_per_lap:
                if reduction and config.restriction_energy_response is None:
                    lap_warnings.append("restricted energy response unavailable")
                    power = original.battery_power_w
                elif original.battery_power_w > 0:
                    power = original.battery_power_w * max(0.0, 1 - (reduction or 0) * (config.restriction_energy_response or 0))
                else:
                    power = original.battery_power_w
                energy_samples.append(replace(original, battery_power_w=power))
            energy = integrate_energy(energy_samples, battery, usable_capacity_j=config.usable_battery_j)
            battery = energy.ending_j
            lap_warnings.extend(energy.warnings)
        else:
            lap_warnings.append("battery starting state or energy trace unavailable")

        if fuel is not None and config.fuel_burn_l_per_lap is not None:
            fuel -= config.fuel_burn_l_per_lap
            if fuel < -1e-9:
                lap_warnings.append("fuel exhausted before projected stint end")
        else:
            lap_warnings.append("fuel quantity or burn unavailable")

        components_available = all((config.base_lap_time_s is not None,
                                    fuel_start is not None, config.fuel_density_kg_per_l is not None,
                                    config.fuel_seconds_per_kg is not None,
                                    initial_penalty is not None, current_penalty is not None,
                                    battery_start is not None, config.energy_pace_delta_s_per_lap is not None,
                                    reduction is not None, config.derate_seconds_at_full_reduction is not None))
        if any("unavailable" in item or "exhausted" in item or "infeasible" in item for item in lap_warnings):
            components_available = False
        if components_available:
            # Fuel baseline is the known initial state, not an unrelated native
            # speed trace. Tire correction is an increment from the same state.
            fuel_correction = (fuel_start - config.starting_fuel_l) * config.fuel_density_kg_per_l * config.fuel_seconds_per_kg
            tire_correction = current_penalty - initial_penalty
            derate_correction = reduction * config.derate_seconds_at_full_reduction
            lap_time = config.base_lap_time_s + fuel_correction + tire_correction + config.energy_pace_delta_s_per_lap + derate_correction
            if lap_time <= 0:
                lap_warnings.append("nonpositive projected lap time")
                lap_time = None
        else:
            lap_time = None
            lap_warnings.append("total lap time unavailable: required model input missing")
        lap_results.append(StintLap(number, lap_time, fuel_start, fuel, battery_start, battery,
                                   dict(tires), None if thermal is None else dict(thermal.temperatures_c),
                                   reduction, tuple(dict.fromkeys(lap_warnings))))
    total = sum(lap.predicted_time_s for lap in lap_results) if all(lap.predicted_time_s is not None for lap in lap_results) else None
    if total is None:
        warnings.append("total time unavailable; inspect per-lap missing inputs")
    return StintResult(tuple(lap_results), total, "stint_projection", "analysis_only",
                       derived_evidence(config.evidence), tuple(dict.fromkeys(warnings)))
