"""Unit tests for airnow.observation_from_row -- the pure mapping from a
stored InfluxDB row to the current-observation shape the dashboard renders --
plus _fetch's handling of AirNow's -1 "not computed" AQI sentinel (the HTTP
call itself is monkeypatched; _fetch_forecast isn't exercised here)."""
import airnow


def test_observation_from_row_none():
    assert airnow.observation_from_row(None) is None


def test_observation_from_row_without_aqi_is_none():
    assert airnow.observation_from_row({"category": "Good"}) is None


def test_observation_from_row_shape():
    row = {
        "aqi": 42.4,
        "category": "Good",
        "dominant_pollutant": "PM2.5",
        "reporting_area": "Rhinelander, WI",
        "pm2_5_aqi": 42.4,
        "o3_aqi": 30,
        "time": "2026-07-20T12:00:00+00:00",
    }
    obs = airnow.observation_from_row(row)
    assert obs["aqi"] == 42  # rounded to int
    assert obs["band"] == "good"
    assert obs["category"] == "Good"
    assert obs["dominant_pollutant"] == "PM2.5"
    assert obs["reporting_area"] == "Rhinelander, WI"
    # Only fields present in the row become pollutant rows, in table order.
    assert obs["pollutants"] == [
        {"parameter": "PM2.5", "aqi": 42, "category": None},
        {"parameter": "O3", "aqi": 30, "category": None},
    ]


def _airnow_reading(parameter, aqi, category_number, category_name):
    return {
        "ParameterName": parameter, "AQI": aqi,
        "Category": {"Number": category_number, "Name": category_name},
        "ReportingArea": "Rhinelander", "StateCode": "WI",
        "Latitude": 45.6, "Longitude": -89.4, "HourObserved": 10,
    }


def _patch_fetch(monkeypatch, readings):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return readings

    monkeypatch.setenv("AIRNOW_API_KEY", "test-key")
    monkeypatch.setattr(airnow.requests, "get", lambda *a, **k: _Resp())


def test_fetch_uncomputed_aqi_is_none_not_negative(monkeypatch):
    """Regression: AQI -1 means "not computed" (common on an active alert
    day), and passing it through made band_for_aqi read it as a healthy
    "good" -- the forecast path already guarded this, current conditions
    didn't."""
    _patch_fetch(monkeypatch, [
        _airnow_reading("PM2.5", -1, 4, "Unhealthy"),
        _airnow_reading("O3", -1, 1, "Good"),
    ])
    obs = airnow._fetch("54501")
    assert obs["aqi"] is None
    assert obs["band"] == "bad"  # from Category.Number, not the -1
    assert obs["dominant_pollutant"] == "PM2.5"  # ranked by category, not AQI
    assert [p["aqi"] for p in obs["pollutants"]] == [None, None]


def test_fetch_real_aqi_outranks_uncomputed(monkeypatch):
    _patch_fetch(monkeypatch, [
        _airnow_reading("PM2.5", -1, 1, "Good"),
        _airnow_reading("O3", 62, 2, "Moderate"),
    ])
    obs = airnow._fetch("54501")
    assert obs["aqi"] == 62
    assert obs["band"] == "fair"
    assert obs["dominant_pollutant"] == "O3"


def test_format_reporting_area_regional_gets_full_state_name():
    # A compass-direction "city" reads as a fake town with "Southeast, MI", so
    # the state name is folded in instead.
    assert airnow._format_reporting_area("Southeast", "MI") == "Southeast Michigan"
    # A normal city keeps the "City, ST" abbreviation.
    assert airnow._format_reporting_area("Rhinelander", "WI") == "Rhinelander, WI"


def test_clean_discussion_strips_agency_heading_and_crlf():
    # AirNow prefixes its own heading and uses CRLF; the UI already labels this
    # text with a "Forecaster's discussion" disclosure, and a stray \r renders
    # as a blank line under the CSS's white-space: pre-line.
    raw = "FORECAST DISCUSSION: \r\nAugust is upon us.\r\nOzone stays Moderate.\r\n"
    assert airnow._clean_discussion(raw) == "August is upon us.\nOzone stays Moderate."


def test_clean_discussion_leaves_text_without_a_heading_alone():
    assert airnow._clean_discussion("  Ozone stays Moderate.  ") == "Ozone stays Moderate."
