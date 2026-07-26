"""Unit tests for the EPA AQI math in epa_aqi.py -- the interpolation, the
rounded-breakpoint gap handling, category names, and the severity bands. These
are the single Python source of truth every provider leans on, so the edges
here (band boundaries, the -1/category forecast fallback) matter."""
import epa_aqi


def test_aqi_at_breakpoint_edges():
    # Each table's first row maps its high concentration straight to AQI 50.
    assert epa_aqi.aqi_from_concentration("PM2.5", 9.0) == 50
    assert epa_aqi.aqi_from_concentration("PM10", 54) == 50
    assert epa_aqi.aqi_from_concentration("O3", 54) == 50
    assert epa_aqi.aqi_from_concentration("CO", 4.4) == 50


def test_aqi_zero_and_negative():
    assert epa_aqi.aqi_from_concentration("PM2.5", 0) == 0
    # Clamped at 0 rather than going negative on a sub-zero reading.
    assert epa_aqi.aqi_from_concentration("PM2.5", -5) == 0


def test_aqi_in_gap_between_rounded_buckets():
    # 9.05 falls in the hairline gap between the first bucket's high (9.0) and
    # the second's low (9.1); it should still resolve (to the lower bucket)
    # rather than returning None.
    assert epa_aqi.aqi_from_concentration("PM2.5", 9.05) == 50


def test_aqi_above_top_bucket_clamps_to_500():
    assert epa_aqi.aqi_from_concentration("PM2.5", 10000) == 500


def test_aqi_unknown_or_missing():
    assert epa_aqi.aqi_from_concentration("XYZ", 10) is None
    assert epa_aqi.aqi_from_concentration("PM2.5", None) is None


def test_category_name_boundaries():
    assert epa_aqi.category_name(50) == "Good"
    assert epa_aqi.category_name(51) == "Moderate"
    assert epa_aqi.category_name(100) == "Moderate"
    assert epa_aqi.category_name(101) == "Unhealthy for Sensitive Groups"
    assert epa_aqi.category_name(200) == "Unhealthy"
    assert epa_aqi.category_name(301) == "Hazardous"
    assert epa_aqi.category_name(None) is None


def test_band_for_aqi_boundaries():
    assert epa_aqi.band_for_aqi(50) == "good"
    assert epa_aqi.band_for_aqi(51) == "fair"
    assert epa_aqi.band_for_aqi(100) == "fair"
    assert epa_aqi.band_for_aqi(101) == "poor"
    assert epa_aqi.band_for_aqi(150) == "poor"
    assert epa_aqi.band_for_aqi(151) == "bad"
    assert epa_aqi.band_for_aqi(None) is None


def test_band_for_category_matches_band_for_aqi_scale():
    assert epa_aqi.band_for_category(1) == "good"
    assert epa_aqi.band_for_category(2) == "fair"
    assert epa_aqi.band_for_category(3) == "poor"
    assert epa_aqi.band_for_category(4) == "bad"
    assert epa_aqi.band_for_category(6) == "bad"
    assert epa_aqi.band_for_category(None) is None


def test_ugm3_to_ppb():
    # ppb = ugm3 * 24.45 / MW; for O3 (MW 48) an input of 48 -> 24.45.
    assert epa_aqi.ugm3_to_ppb("O3", 48.0) == 24.45
    assert epa_aqi.ugm3_to_ppb("O3", None) is None
    assert epa_aqi.ugm3_to_ppb("NH3-unknown", 10) is None


def test_epa_value_normalizes_provider_units():
    # Particulates pass through as reported (already the table's µg/m³).
    assert epa_aqi.epa_value("PM2.5", 12.0, "MICROGRAMS_PER_CUBIC_METER") == 12.0
    # ppb gases pass through; µg/m³ gases convert.
    assert epa_aqi.epa_value("O3", 60.0, "PARTS_PER_BILLION") == 60.0
    assert epa_aqi.epa_value("O3", 48.0, "MICROGRAMS_PER_CUBIC_METER") == 24.45
    # CO is the trap: its table is ppm, both provider units are 1000x bigger.
    assert epa_aqi.epa_value("CO", 210.0, "PARTS_PER_BILLION") == 0.21
    assert round(epa_aqi.epa_value("CO", 229.14, "MICROGRAMS_PER_CUBIC_METER"), 3) == 0.2
    assert epa_aqi.epa_value("CO", None, "PARTS_PER_BILLION") is None
    assert epa_aqi.epa_value("NH3", 10.0, "MICROGRAMS_PER_CUBIC_METER")  # no breakpoints, still converts
    assert epa_aqi.epa_value("Unknown", 10.0, "MICROGRAMS_PER_CUBIC_METER") is None
