"""
generation.py — Phase 2: trip production and attraction per TAZ.

Production P_i ~ residential population of zone i.
Attraction A_j ~ employment in zone j.

**Synthetic baseline (plan §Phase 2, known limitation):** census ward population
(task 0.8) is not yet loaded, so these are plausible order-of-magnitude estimates
for the WEH suburbs, structured so real census/employment data drops in later.
Job attraction is deliberately weighted toward Andheri (MIDC/SEEPZ), Bandra (BKC-
adjacent), and the airport belt (Vile Parle/Santacruz), reproducing the classic
north-residential -> south-jobs commute that loads the WEH southbound in the AM peak.

Returns a DataFrame indexed by zone_id with population, employment, and the
balanced production/attraction vectors (in peak-hour person-trips).
"""

from __future__ import annotations

import pandas as pd

# (zone_id, name, population, employment) — SYNTHETIC placeholders (thousands of people).
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

# Peak-hour trip rate: person-trips generated per resident in the peak hour.
# Synthetic; calibrate against CTS/IRC guidance (docs/calibration_log.md).
PEAK_TRIP_RATE = 0.12


def zonal_socioeconomics() -> pd.DataFrame:
    df = pd.DataFrame(ZONE_SOCIOECONOMICS,
                      columns=["zone_id", "name", "population_k", "employment_k"])
    df = df.set_index("zone_id")
    return df


def production_attraction(production_scale=1.0, attraction_scale=1.0,
                          processing_rate: float | None = None) -> pd.DataFrame:
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
    df = zonal_socioeconomics()
    df["P"] = df["population_k"] * PEAK_TRIP_RATE * production_scale   # outflow rate
    df["A"] = df["employment_k"].astype(float) * attraction_scale     # inflow rate (shape)

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
    print(f"\nTotal production (person-trips/peak-hr): {df['P'].sum():.0f}")
    print(f"Total attraction (person-trips/peak-hr): {df['A'].sum():.0f}")
