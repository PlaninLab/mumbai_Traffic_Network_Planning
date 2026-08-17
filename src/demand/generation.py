"""
generation.py — Phase 2: trip production and attraction per TAZ.

Production P_i ~ residential population of zone i.
Attraction A_j ~ employment in zone j.

**CTS-controlled synthetic baseline:** census ward population (task 0.8) is not
yet loaded, so the locality allocation remains synthetic.  The allocation is
scaled to the Western Suburbs control totals reported by MMRDA's Updation of
Comprehensive Transportation Study (CTSU scenario, Tables 5-2 and 5-4).
Job attraction is deliberately weighted toward Andheri (MIDC/SEEPZ), Bandra (BKC-
adjacent), and the airport belt (Vile Parle/Santacruz), reproducing the classic
north-residential -> south-jobs commute that loads the WEH southbound in the AM peak.

Returns a DataFrame indexed by zone_id with population, employment, and the
balanced production/attraction vectors (in peak-hour person-trips).
"""

from __future__ import annotations

import pandas as pd

# (zone_id, name, population, employment) — synthetic allocation weights.  The
# last two fields started as thousands of people, but are now treated only as
# locality shares and scaled to the CTS Western Suburbs totals below.
ZONE_SOCIOECONOMICS = [
    (0,  "Dahisar",    350,  40),
    (1,  "Borivali",   450,  70),
    (2,  "Kandivali",  500,  80),
    (3,  "Malad",      480, 120),   # Mindspace IT park
    (4,  "Goregaon",   400, 150),   # IT park / film city belt
    (5,  "Jogeshwari", 350,  90),
    (6,  "Andheri",    700, 350),   # MIDC, SEEPZ — major job center
    (7,  "Vile Parle", 250, 120),   # airport-adjacent
    (8,  "Santacruz",  220, 130),   # airport / Kalina
    (9,  "Khar",       180,  90),
    (10, "Bandra",     300, 250),   # BKC-adjacent commercial
]

# MMRDA CTSU Western Suburbs planning controls, in thousands of people/jobs.
# 2017 is the validated base year; later years are CTSU scenario forecasts.
CTS_WESTERN_SUBURBS_CONTROLS = {
    2017: {"population_k": 5_750.0, "employment_k": 2_600.0},
    2021: {"population_k": 5_950.0, "employment_k": 2_750.0},
    2026: {"population_k": 6_010.0, "employment_k": 2_890.0},
    2031: {"population_k": 6_100.0, "employment_k": 2_980.0},
    2041: {"population_k": 6_120.0, "employment_k": 3_120.0},
}
DEFAULT_CTS_CONTROL_YEAR = 2026

# Peak-hour trip rate: person-trips generated per resident in the peak hour.
# Synthetic; calibrate against CTS/IRC guidance (docs/calibration_log.md).
PEAK_TRIP_RATE = 0.12


def zonal_socioeconomics(control_year: int = DEFAULT_CTS_CONTROL_YEAR) -> pd.DataFrame:
    """Return locality weights scaled to CTS Western Suburbs control totals.

    The PDF does not publish its 1,810-zone planning-parameter table, so the
    within-corridor allocation is still the documented synthetic one.  Scaling
    makes the aggregate population and employment evidence-based while keeping
    that allocation explicit and replaceable when the CTS TAZ data is obtained.
    """
    if control_year not in CTS_WESTERN_SUBURBS_CONTROLS:
        valid = ", ".join(str(y) for y in CTS_WESTERN_SUBURBS_CONTROLS)
        raise ValueError(f"Unknown CTS control year {control_year}; choose one of: {valid}")

    df = pd.DataFrame(ZONE_SOCIOECONOMICS,
                      columns=["zone_id", "name", "population_k", "employment_k"])
    df = df.set_index("zone_id")
    df["population_weight_k"] = df["population_k"].astype(float)
    df["employment_weight_k"] = df["employment_k"].astype(float)

    controls = CTS_WESTERN_SUBURBS_CONTROLS[control_year]
    df["population_k"] *= controls["population_k"] / df["population_k"].sum()
    df["employment_k"] *= controls["employment_k"] / df["employment_k"].sum()
    df.attrs.update({
        "control_source": "MMRDA CTSU Tables 5-2 and 5-4",
        "control_year": control_year,
        **controls,
    })
    return df


def production_attraction(production_scale=1.0, attraction_scale=1.0,
                          processing_rate: float | None = None,
                          control_year: int = DEFAULT_CTS_CONTROL_YEAR) -> pd.DataFrame:
    """Compute peak-hour production P_i (outflow) and attraction A_j (inflow).

    Robustness parameters (see docs/assumptions.md):
      production_scale : scalar or per-zone array — scales OUTGOING trip rate (P_i).
      attraction_scale : scalar or per-zone array — scales INCOMING trip rate (A_j).
      processing_rate  : optional per-zone ceiling (person-trips/hr) on how much a
                         region can emit OR absorb — its throughput / "processing rate".
                         Caps both P_i and A_j; models a gateway/discharge limit.

    In a doubly-constrained gravity model total productions must equal total
    attractions, so after any capping we rescale attractions to the production total.
    """
    df = zonal_socioeconomics(control_year=control_year)
    # Convert the published thousand-person controls to people before applying
    # rates, so P/A retain their documented person-trips/hour units.
    df["P"] = df["population_k"] * 1000.0 * PEAK_TRIP_RATE * production_scale
    df["A"] = df["employment_k"] * 1000.0 * attraction_scale

    if processing_rate is not None:
        df["P"] = df["P"].clip(upper=processing_rate)
        df["A"] = df["A"].clip(upper=processing_rate)

    # Balance attractions to the production total (doubly-constrained requirement).
    df["A"] = df["A"] * (df["P"].sum() / df["A"].sum())
    return df


if __name__ == "__main__":
    df = production_attraction()
    pd.set_option("display.width", 100)
    print(df[["name", "population_k", "employment_k", "P", "A"]].round(1))
    print(f"\nCTS Western Suburbs control year: {df.attrs['control_year']}")
    print(f"Population control: {df['population_k'].sum():.0f} thousand")
    print(f"Employment control: {df['employment_k'].sum():.0f} thousand")
    print(f"\nTotal production (person-trips/peak-hr): {df['P'].sum():.0f}")
    print(f"Total attraction (person-trips/peak-hr): {df['A'].sum():.0f}")
