"""Tests for measured road width -> capacity (Phase 0)."""

from src.network.road_width import STD_LANE_WIDTH_M, capacity_from_width


def test_measured_effective_width_sets_capacity():
    # primary road: 1800 PCU/lane, 3.5 m lane. 10.5 m effective = 3 lanes worth.
    c = capacity_from_width(14.0, 10.5, road_class="primary")
    assert c["capacity_pcu_hr"] == round((10.5 / STD_LANE_WIDTH_M) * 1800, 1)
    assert c["capacity_nominal_pcu_hr"] == round((14.0 / STD_LANE_WIDTH_M) * 1800, 1)
    assert c["effective_width_source"] == "measured"
    assert c["encroachment_factor"] == round(10.5 / 14.0, 3)


def test_total_only_falls_back_to_085():
    c = capacity_from_width(11.0, None, road_class="primary")
    assert c["effective_width_source"] == "fallback_0.85"
    assert c["encroachment_factor"] == 0.85
    # usable = total * 0.85, in capacity terms
    assert c["capacity_pcu_hr"] == round((11.0 * 0.85 / STD_LANE_WIDTH_M) * 1800, 1)


def test_effective_cannot_exceed_total():
    c = capacity_from_width(7.0, 9.0, road_class="secondary")   # eff > total -> clamp
    assert c["effective_width_m"] == 7.0
    assert c["capacity_pcu_hr"] == c["capacity_nominal_pcu_hr"]


def test_road_class_changes_per_lane_rate():
    mway = capacity_from_width(14.0, 10.5, road_class="motorway")   # 2000/lane
    sec = capacity_from_width(14.0, 10.5, road_class="secondary")   # 1500/lane
    assert mway["capacity_pcu_hr"] > sec["capacity_pcu_hr"]
