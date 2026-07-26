"""Unit tests for aq_shared.py's Away-facing helpers: turning a flat history
point into the same current-conditions shape the DB-backed providers already
return (see away.current), and trimming a point list to a trailing time
window (see /api/outside/history?mode=away)."""
from datetime import UTC, datetime, timedelta

import aq_shared


def test_point_to_pollutants_skips_missing_fields():
    point = {"pm2_5_ugm3": 12.0, "o3_ppb": None}
    field_defs = [
        ("pm2_5_ugm3", "PM2.5", "MICROGRAMS_PER_CUBIC_METER"),
        ("o3_ppb", "O3", "PARTS_PER_BILLION"),
    ]
    assert aq_shared.point_to_pollutants(point, field_defs) == [
        {"parameter": "PM2.5", "concentration_value": 12.0, "concentration_units": "MICROGRAMS_PER_CUBIC_METER"},
    ]


def test_dominant_from_pollutants_picks_worst_aqi():
    pollutants = [
        {"parameter": "PM2.5", "concentration_value": 200.0, "concentration_units": "MICROGRAMS_PER_CUBIC_METER"},
        {"parameter": "O3", "concentration_value": 30.0, "concentration_units": "PARTS_PER_BILLION"},
    ]
    assert aq_shared.dominant_from_pollutants(pollutants) == "PM2.5"


def test_dominant_from_pollutants_converts_co_ppb_to_ppm():
    """Regression: EPA's CO breakpoints are ppm but every provider reports CO
    in ppb, so an ordinary ~200 ppb reading used to be read as 200 ppm --
    off the end of a table that stops at 15.4 -- clamping to AQI 500 and
    making CO the "Driven by" pollutant on every Away reading."""
    pollutants = [
        {"parameter": "PM2.5", "concentration_value": 12.0, "concentration_units": "MICROGRAMS_PER_CUBIC_METER"},
        {"parameter": "O3", "concentration_value": 60.0, "concentration_units": "PARTS_PER_BILLION"},
        {"parameter": "CO", "concentration_value": 210.0, "concentration_units": "PARTS_PER_BILLION"},
    ]
    assert aq_shared.dominant_from_pollutants(pollutants) == "O3"


def test_dominant_from_pollutants_empty_is_none():
    assert aq_shared.dominant_from_pollutants([]) is None


def test_away_observation_shape():
    point = {"time": "2026-07-20T10:00:00Z", "aqi": 42, "pm2_5_ugm3": 12.0}
    field_defs = [("pm2_5_ugm3", "PM2.5", "MICROGRAMS_PER_CUBIC_METER")]
    obs = aq_shared.away_observation(point, field_defs, reporting_area="Chicago")
    assert obs["aqi"] == 42
    assert obs["reporting_area"] == "Chicago"
    assert obs["observed_hour"] is None
    assert obs["time"] == "2026-07-20T10:00:00Z"
    assert obs["category"]
    assert obs["band"]
    assert obs["pollutants"][0]["parameter"] == "PM2.5"


def test_away_observation_no_aqi_is_none():
    assert aq_shared.away_observation({"time": "t"}, [], "X") is None


def test_points_since_handles_z_and_offset_timestamp_formats():
    now = datetime.now(UTC)
    old = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    recent = (now - timedelta(hours=1)).isoformat()
    points = [{"time": old, "aqi": 1}, {"time": recent, "aqi": 2}]
    assert aq_shared.points_since(points, 24) == [{"time": recent, "aqi": 2}]


def test_points_since_handles_naive_timestamps():
    # AirNow's away history builds timestamps from DateObserved/HourObserved
    # with no timezone marker at all -- used to crash comparing a naive
    # datetime against the (aware) cutoff. Assumed UTC rather than raising.
    now = datetime.now(UTC)
    old_naive = (now - timedelta(days=5)).replace(tzinfo=None).isoformat()
    recent_naive = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    points = [{"time": old_naive, "aqi": 1}, {"time": recent_naive, "aqi": 2}]
    assert aq_shared.points_since(points, 24) == [{"time": recent_naive, "aqi": 2}]


def test_points_since_drops_points_with_no_time():
    assert aq_shared.points_since([{"aqi": 1}], 24) == []
