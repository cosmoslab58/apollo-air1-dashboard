"""Guards the DB-backed provider current-observation builders (google_aq/
owm/purpleair), which now share aq_shared.observation. These lock in the
per-provider behavior the shared builder must preserve: PurpleAir keeps raw
(unrounded) concentrations, OWM appends NH3 as concentration-only and falls
back to a computed category, Google always carries a health_recommendations
key. InfluxDB reads are monkeypatched -- no DB is touched."""
import google_aq
import influx
import owm
import purpleair


def test_purpleair_keeps_raw_concentration_and_sensor_name(monkeypatch):
    monkeypatch.setattr(influx, "query_purpleair_latest", lambda: {
        "purpleair_aqi_epa": 42.7, "purpleair_category": "Good",
        "purpleair_dominant_pollutant": "PM2.5",
        "purpleair_pm2_5_ugm3": 9.876543, "purpleair_pm2_5_aqi_epa": 42.7,
        "time": "T",
    })
    obs = purpleair.get_current_observation()
    assert obs["aqi"] == 43  # headline rounded to int
    assert obs["band"] == "good"
    assert obs["reporting_area"] == purpleair.SENSOR_NAME
    # PurpleAir passes the corrected concentration through un-rounded.
    assert obs["pollutants"][0]["concentration_value"] == 9.876543
    assert obs["pollutants"][0]["aqi"] == 43


def test_owm_appends_nh3_and_falls_back_to_computed_category(monkeypatch):
    monkeypatch.setattr(influx, "query_owm_latest", lambda: {
        "owm_aqi_epa": 55.0, "owm_category": None, "owm_dominant_pollutant": None,
        "owm_pm2_5_ugm3": 15.678, "owm_pm2_5_aqi_epa": 55.0,
        "owm_nh3_ugm3": 3.14159, "time": "T",
    })
    obs = owm.get_current_observation()
    assert obs["category"] == "Moderate"  # computed from AQI when unstored
    assert obs["pollutants"][0]["concentration_value"] == 15.68  # rounded to 2dp
    nh3 = obs["pollutants"][-1]
    assert nh3["parameter"] == "NH3"
    assert "aqi" not in nh3  # NH3 has no EPA breakpoint -> concentration only


def test_google_always_carries_health_key(monkeypatch):
    monkeypatch.setattr(influx, "query_google_latest", lambda: {
        "google_aqi_epa": 30.0, "google_category": "Good",
        "google_dominant_pollutant": "O3",
        "google_health_general": "enjoy the outdoors",
        "google_health_children": "ok for kids", "time": "T",
    })
    obs = google_aq.get_current_observation()
    assert obs["reporting_area"] is None  # caller fills the home label
    assert obs["health_recommendations"] == {
        "generalPopulation": "enjoy the outdoors",
        "children": "ok for kids",
    }


def test_google_health_key_present_when_absent(monkeypatch):
    monkeypatch.setattr(influx, "query_google_latest", lambda: {"google_aqi_epa": 30.0, "time": "T"})
    obs = google_aq.get_current_observation()
    assert obs["health_recommendations"] is None


def test_owm_forecast_dominant_breaks_ties_by_component_order():
    """Regression: a max() over (aqi, parameter) tuples fell through to
    comparing the parameter *name* on a tie, so a clean day where several
    sub-indices round to the same low number reported the alphabetically-last
    pollutant ("Driven by SO2"). Ties keep FORECAST_COMPONENTS order (PM2.5
    first), matching every other provider's worst-of."""
    # 0.9 µg/m³ PM2.5 and 9.2 µg/m³ SO2 both compute to AQI 5.
    components = {"pm2_5": 0.9, "pm10": 0.0, "o3": 0.0, "no2": 0.0, "so2": 9.2, "co": 0.0}
    assert owm._aqi_for("PM2.5", 0.9) == owm._aqi_for("SO2", 9.2)  # the tie itself
    assert owm._forecast_aqi_dominant(components) == (5, "PM2.5")


def test_owm_forecast_dominant_none_when_nothing_converts():
    assert owm._forecast_aqi_dominant({}) is None


def test_missing_row_returns_none(monkeypatch):
    monkeypatch.setattr(influx, "query_owm_latest", lambda: None)
    assert owm.get_current_observation() is None


def test_no_headline_aqi_returns_none(monkeypatch):
    # A row with no stored AQI has nothing to headline -> None, not a 0-AQI card.
    monkeypatch.setattr(influx, "query_purpleair_latest", lambda: {"time": "T"})
    assert purpleair.get_current_observation() is None
