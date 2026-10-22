"""Small known examples and failure boundaries for deterministic models."""

import unittest

from wimna.models import (
    EnergySample, ModelParameter, StintConfig, ThermalConfig, ThermalController,
    ThermalNode, ThermalSample, ThermalScenario, ThermalState, TireConfig,
    TireSample, TireState, batch_thermal_scenarios, case_scenario_presets, curve_value, derived_evidence,
    fuel_consumption, fuel_local_contrast, fuel_mass_kg, integrate_energy,
    project_energy_repayment, project_stint, project_tire_step,
    run_thermal_scenario, signed_battery_power, step_thermal_system,
    thermal_assumption_profiles, tire_pace_penalty_s, warm_start_state,
)


def simple_thermal(*, heat_w=100.0, cooling=0.0, threshold=85.0, timeout=10.0):
    return ThermalConfig(
        {"electrical": ThermalNode(100.0, heat_w, cooling, 0.0)},
        "electrical",
        ThermalController(threshold - 10, threshold, threshold - 5, 0.2, 0.1, timeout),
    )


class CurveTireTests(unittest.TestCase):
    def test_curve_piecewise_and_boundary_warning(self):
        points = ((0.0, 0.0), (0.5, 1.0), (1.0, 10.0))
        self.assertEqual(curve_value(points, 0.75).value, 5.5)
        lower = curve_value(points, -0.1)
        self.assertEqual(lower.value, 0.0)
        self.assertTrue(lower.clamped)
        upper = curve_value(points, 1.1, extrapolate=True)
        self.assertAlmostEqual(upper.value, 11.8)
        self.assertTrue(upper.extrapolated)
        with self.assertRaises(ValueError):
            curve_value(((0, 1), (0, 2)), 0.1)

    def test_tire_workload_wear_temperature_and_penalty(self):
        config = TireConfig(0.001, 0.01, 0.0, 2.0, 0.5,
                            ((0, 0), (0.5, 1), (1, 10)),
                            ((0, 0), (100, 0)))
        state = TireState(0.1, 30.0)
        sample = TireSample(2, 10, 1, 0.2, 20, 30)
        step = project_tire_step(state, sample, config)
        self.assertAlmostEqual(step.workload_m, 28.0)
        self.assertAlmostEqual(step.state.wear_fraction, 0.128)
        self.assertAlmostEqual(step.state.temperature_c, 30.28)
        self.assertAlmostEqual(tire_pace_penalty_s(step.state, config).value, 0.256)

    def test_tire_cooling_timestep_convergence(self):
        config = TireConfig(0, 0, 0.2, 0, 0)
        one = project_tire_step(TireState(0, 100), TireSample(10, 0, 0, 0, 20, 30), config).state
        many = TireState(0, 100)
        for _ in range(10):
            many = project_tire_step(many, TireSample(1, 0, 0, 0, 20, 30), config).state
        self.assertAlmostEqual(one.temperature_c, many.temperature_c, places=10)


class FuelEnergyTests(unittest.TestCase):
    def test_fuel_unit_and_local_slope(self):
        self.assertAlmostEqual(fuel_consumption(10, 8, 2), 1)
        self.assertIsNone(fuel_mass_kg(10, None))
        contrast = fuel_local_contrast((100, 101), (102, 103), 20, 30,
                                       density_kg_per_l=0.8, tire_matched=True,
                                       battery_matched=True)
        self.assertAlmostEqual(contrast.seconds_per_litre, 0.2)
        self.assertAlmostEqual(contrast.seconds_per_kg, 0.25)
        self.assertTrue(contrast.exploratory)
        unmatched = fuel_local_contrast((100,), (102,), 20, 30,
                                        density_kg_per_l=None, tire_matched=False,
                                        battery_matched=True)
        self.assertIsNone(unmatched.seconds_per_litre)
        with self.assertRaises(ValueError):
            fuel_local_contrast((100,), (102,), 20, 30,
                                density_kg_per_l=0, tire_matched=True,
                                battery_matched=True)

    def test_signed_battery_and_debt(self):
        self.assertEqual(signed_battery_power(-100, positive_means="recovery"), 100)
        result = integrate_energy((EnergySample(2, 1000, 1100, 900),
                                   EnergySample(2, -500, 0, 0)), 5000,
                                  usable_capacity_j=10000)
        self.assertEqual(result.deployed_j, 2000)
        self.assertEqual(result.recovered_j, 1000)
        self.assertEqual(result.ending_j, 4000)
        self.assertEqual(result.requested_j, 2200)
        self.assertEqual(result.delivered_j, 1800)
        limited = integrate_energy((EnergySample(2, 1000),), 5000,
                                   deployment_allowance_j=1000)
        self.assertIn("deployment exceeds declared allowance", limited.warnings)
        self.assertEqual(project_energy_repayment(1000, net_recovery_j_per_lap=250,
                                                  cost_s_per_recovery_lap=2).projected_time_cost_s, 8)
        self.assertIsNone(project_energy_repayment(1000, net_recovery_j_per_lap=None,
                                                   cost_s_per_recovery_lap=2).projected_time_cost_s)


class ThermalTests(unittest.TestCase):
    def test_heat_balance_and_cooling(self):
        config = simple_thermal()
        start = ThermalState({"electrical": 20})
        hot = step_thermal_system(start, ThermalSample(1, 1, 0, 20), config)
        self.assertAlmostEqual(hot.state.temperatures_c["electrical"], 21)
        self.assertAlmostEqual(hot.generated_j, 100)
        self.assertAlmostEqual(hot.ambient_loss_j, 0)
        cooling_config = simple_thermal(heat_w=0, cooling=5)
        cool = step_thermal_system(ThermalState({"electrical": 80}),
                                   ThermalSample(1, 0, 0, 20), cooling_config)
        self.assertLess(cool.state.temperatures_c["electrical"], 80)
        self.assertGreater(cool.ambient_loss_j, 0)

    def test_hysteresis_timeout_and_no_false_live_effect(self):
        config = simple_thermal(heat_w=0, threshold=85, timeout=3)
        state = ThermalState({"electrical": 86})
        first = step_thermal_system(state, ThermalSample(1, 0, 0, 20), config)
        self.assertAlmostEqual(first.requested_reduction_fraction, 0.1)
        self.assertEqual(first.physical_effect_status, "analysis_only")
        second = step_thermal_system(first.state, ThermalSample(1, 0, 0, 20,
                                      actuation_supported=True), config)
        self.assertEqual(second.physical_effect_status, "unverified")
        self.assertAlmostEqual(second.requested_reduction_fraction, 0.2)
        third = step_thermal_system(second.state, ThermalSample(1, 0, 0, 20), config)
        self.assertTrue(third.state.timed_out)
        self.assertFalse(third.state.controller_active)
        recovered = step_thermal_system(
            ThermalState({"electrical": 79}, third.state.reduction_fraction,
                         False, third.state.active_s, True),
            ThermalSample(1, 0, 0, 20), config)
        self.assertFalse(recovered.state.timed_out)
        with self.assertRaises(ValueError):
            ThermalSample(1, 0, 0, 20, effect_verified=True)
        verified = step_thermal_system(
            state,
            ThermalSample(1, 0, 0, 20, actuation_supported=True,
                          observed_reduction_fraction=0.1, effect_verified=True,
                          battery_limit_excluded=True, rule_limit_excluded=True,
                          baseline_comparable=True), config)
        self.assertEqual(verified.physical_effect_status, "verified")
        mismatched = step_thermal_system(
            state,
            ThermalSample(1, 0, 0, 20, actuation_supported=True,
                          observed_reduction_fraction=0, effect_verified=True,
                          battery_limit_excluded=True, rule_limit_excluded=True,
                          baseline_comparable=True), config)
        self.assertEqual(mismatched.physical_effect_status, "unverified")

    def test_scenario_requires_air_and_stays_analysis_only(self):
        config = simple_thermal()
        trace = (ThermalSample(1, 1, 20, 20),)
        unavailable = run_thermal_scenario(trace, config, ThermalScenario("R4", 29, None))
        self.assertFalse(unavailable.available)
        self.assertFalse(run_thermal_scenario((), config, ThermalScenario("R4", 29, 20)).available)
        result = run_thermal_scenario(trace, config, ThermalScenario("R4", 29, 20), repeats=2)
        self.assertEqual(result.physical_effect_status, "analysis_only")
        self.assertAlmostEqual(result.peak_temperatures_c["electrical"], 22)
        self.assertEqual([s.track_temp_c for s in case_scenario_presets(20)], [29, 33, 36])
        profiles = thermal_assumption_profiles()
        self.assertEqual(set(profiles), {"low", "base", "high"})
        self.assertEqual(profiles["base"].parameter_records["control.derate"].evidence, "ASSUMED")
        self.assertEqual(profiles["base"].parameter_records["control.derate"].low, 78)
        warm = warm_start_state(config, 20)
        self.assertEqual(warm.temperatures_c["electrical"], config.controller.derate_c - 1)
        sweep = batch_thermal_scenarios(trace, {"base": config}, case_scenario_presets(20))
        self.assertEqual(set(sweep["base"]), {"reference_R4", "elevated_R8", "severe_R5"})
        self.assertEqual(sweep["base"]["severe_R5"].physical_effect_status, "analysis_only")

    def test_thermal_timestep_convergence(self):
        config = simple_thermal(heat_w=100, cooling=5, threshold=1000)
        once = step_thermal_system(ThermalState({"electrical": 40}),
                                   ThermalSample(10, 1, 10, 20), config).state
        many = ThermalState({"electrical": 40})
        for _ in range(10):
            many = step_thermal_system(many, ThermalSample(1, 1, 10, 20), config).state
        self.assertAlmostEqual(once.temperatures_c["electrical"],
                               many.temperatures_c["electrical"], places=10)


class StintAndProvenanceTests(unittest.TestCase):
    def test_parameter_null_and_mixed_provenance(self):
        p = ModelParameter(None, "W", "CSP channel", "0.2.12-preview1", "MEASURED")
        with self.assertRaises(ValueError):
            p.require()
        self.assertEqual(derived_evidence(("MEASURED",), ("ASSUMED",)),
                         ("ASSUMED", "MEASURED", "PHYSICS_DERIVED"))

    def test_stint_projects_state_without_claiming_live_effect(self):
        tire = TireConfig(0.001, 0, 0, 0, 0,
                          ((0, 0), (1, 10)), ((0, 0), (100, 0)))
        config = StintConfig(
            2, 100, 10, 1, 1, 0.1,
            {"FL": TireState(0, 30)},
            {"FL": (TireSample(10, 10, 1, 0, 20, 30),)}, tire,
            1000, 2000, (EnergySample(10, 0),), 0,
            simple_thermal(heat_w=0, threshold=100),
            (ThermalSample(10, 0, 10, 20),),
            derate_seconds_at_full_reduction=10,
        )
        result = project_stint(config)
        self.assertAlmostEqual(result.laps[0].predicted_time_s, 101)
        self.assertAlmostEqual(result.laps[1].predicted_time_s, 101.9)
        self.assertAlmostEqual(result.total_time_s, 202.9)
        self.assertEqual(result.physical_effect_status, "analysis_only")
        self.assertAlmostEqual(result.laps[1].ending_tires["FL"].wear_fraction, 0.2)
        no_curves = StintConfig(**{**config.__dict__, "tire_model": TireConfig(0.001, 0, 0, 0, 0)})
        self.assertIsNone(project_stint(no_curves).total_time_s)


if __name__ == "__main__":
    unittest.main()
