"""Unit tests for airnow.observation_from_row -- the pure mapping from a
stored InfluxDB row to the current-observation shape the dashboard renders --
plus _fetch and _fetch_forecast against the payloads of AirNow's replacement
services (the HTTP call itself is monkeypatched)."""
import pytest

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


def _airnow_reading(parameter, aqi, category_name):
    # The shape of /aq/observation/current/ziplatlong/ (the service that replaced
    # zipCode/current on 2026-10-01): no state code, coordinates or category number.
    return {
        "dateObserved": "2026-11-02", "hourObserved": "10:00", "localTimeZone": "CST",
        "reportingAreaName": "Rhinelander", "siteID": "550850004", "siteName": "Trout Lake",
        "parameterName": parameter, "nowcastAQI": aqi, "aqiCategoryName": category_name,
    }


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _patch_fetch(monkeypatch, readings):
    monkeypatch.setenv("AIRNOW_API_KEY", "test-key")
    seen = []
    monkeypatch.setattr(airnow.requests, "get",
                        lambda url, params=None, **k: seen.append((url, params)) or _Resp(readings))
    return seen


def test_fetch_new_service_shape(monkeypatch):
    seen = _patch_fetch(monkeypatch, [
        _airnow_reading("PM2.5", 42, "Good"),
        _airnow_reading("OZONE", 30, "Good"),
    ])
    obs = airnow._fetch("54501")
    assert seen[0][0].endswith("/aq/observation/current/ziplatlong/")
    assert seen[0][1]["zipCode"] == "54501"
    assert obs["aqi"] == 42 and obs["band"] == "good"
    assert obs["reporting_area"] == "Rhinelander, WI"   # state rebuilt from the site id (55 = WI)
    assert obs["state_code"] == "WI" and obs["reporting_area_name"] == "Rhinelander"
    assert obs["observed_hour"] == 10
    assert [p["parameter"] for p in obs["pollutants"]] == ["PM2.5", "O3"]   # OZONE reads as O3


def test_fetch_without_a_site_id_takes_the_state_from_the_forecast(monkeypatch):
    reading = {**_airnow_reading("PM2.5", 33, "Good"), "siteID": None, "reportingAreaName": "Northwoods East"}
    _patch_fetch(monkeypatch, [reading])
    monkeypatch.setattr(airnow, "get_forecast", lambda zip_code: {"state_code": "WI"})
    obs = airnow._fetch("54554")
    assert obs["state_code"] == "WI" and obs["reporting_area"] == "Northwoods East, WI"


def test_fetch_uncomputed_aqi_is_none_not_negative(monkeypatch):
    """Regression: AQI -1 means "not computed" (common on an active alert
    day), and passing it through made band_for_aqi read it as a healthy
    "good"."""
    _patch_fetch(monkeypatch, [
        _airnow_reading("PM2.5", -1, "Unhealthy"),
        _airnow_reading("OZONE", -1, "Good"),
    ])
    obs = airnow._fetch("54501")
    assert obs["aqi"] is None
    assert obs["band"] == "bad"  # from the category, not the -1
    assert obs["dominant_pollutant"] == "PM2.5"  # ranked by category, not AQI
    assert [p["aqi"] for p in obs["pollutants"]] == [None, None]


def test_fetch_real_aqi_outranks_uncomputed(monkeypatch):
    _patch_fetch(monkeypatch, [
        _airnow_reading("PM2.5", -1, "Good"),
        _airnow_reading("OZONE", 62, "Moderate"),
    ])
    obs = airnow._fetch("54501")
    assert obs["aqi"] == 62
    assert obs["band"] == "fair"
    assert obs["dominant_pollutant"] == "O3"


def test_service_error_object_raises_instead_of_reading_as_no_data(monkeypatch):
    # AirNow answers a retired service or bad key with HTTP 200 and this object;
    # read as "no readings" it hid the 2026-10-01 retirement for a month.
    _patch_fetch(monkeypatch, {"WebServiceError": [{"Message": "This web service has been retired"}]})
    with pytest.raises(RuntimeError, match="retired"):
        airnow._fetch("54501")


def test_forecast_new_service_shape(monkeypatch):
    row = {"dateIssue": "2026-11-02", "reportingArea": "Southeast", "stateCode": "MI",
           "parameterName": "PM2.5", "forecastAgency": "EGLE"}
    seen = _patch_fetch(monkeypatch, [
        {**row, "dateValid": "2026-11-03", "aqi": -1, "categoryNumber": 1, "categoryName": "Good",
         "actionDay": False},
        {**row, "dateValid": "2026-11-02", "aqi": -1, "categoryNumber": 2, "categoryName": "Moderate",
         "actionDay": True, "discussion": "FORECAST DISCUSSION:\r\nWarm and breezy."},
    ])
    fc = airnow._fetch_forecast("48324")
    assert seen[0][0].endswith("/aq/forecast/current/")
    assert fc["reporting_area"] == "Southeast Michigan"
    assert fc["discussion"] == "Warm and breezy."
    assert [d["date"] for d in fc["days"]] == ["2026-11-02", "2026-11-03"]
    assert fc["days"][0]["band"] == "fair" and fc["days"][0]["aqi"] is None
    assert fc["days"][0]["action_day"] is True


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
