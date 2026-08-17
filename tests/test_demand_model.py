from __future__ import annotations

import unittest

from src.demand.generation import (
    CTS_WESTERN_SUBURBS_CONTROLS,
    zonal_socioeconomics,
)
from src.demand.gravity_model import (
    BETA_CALIBRATION_BOUNDS,
    CTS_PRIVATE_TRIP_LENGTH_KM,
    build_od,
)


class CtsDemandControlsTest(unittest.TestCase):
    def test_2026_western_suburbs_control_totals_are_applied(self) -> None:
        zones = zonal_socioeconomics(control_year=2026)
        controls = CTS_WESTERN_SUBURBS_CONTROLS[2026]

        self.assertAlmostEqual(zones["population_k"].sum(), controls["population_k"])
        self.assertAlmostEqual(zones["employment_k"].sum(), controls["employment_k"])
        self.assertEqual(zones.attrs["control_year"], 2026)

    def test_unknown_control_year_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown CTS control year"):
            zonal_socioeconomics(control_year=2025)


class GravityCalibrationIntegrationTest(unittest.TestCase):
    def test_default_beta_matches_cts_trip_length_target(self) -> None:
        zones, _person_trips, vehicle_trips, _costs = build_od(
            target_total_pcu=1000.0,
        )

        lo, hi = BETA_CALIBRATION_BOUNDS
        self.assertGreaterEqual(zones.attrs["gravity_beta"], lo)
        self.assertLessEqual(zones.attrs["gravity_beta"], hi)
        self.assertAlmostEqual(
            zones.attrs["trip_length_achieved_km"],
            CTS_PRIVATE_TRIP_LENGTH_KM,
            places=3,
        )

        interzonal = vehicle_trips.copy()
        interzonal[range(len(interzonal)), range(len(interzonal))] = 0.0
        self.assertAlmostEqual(interzonal.sum(), 1000.0, places=6)


if __name__ == "__main__":
    unittest.main()
