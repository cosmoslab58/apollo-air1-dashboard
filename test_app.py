"""Route-level tests for app.py: the error-status mapping (InfluxDB failure ->
502, no data -> 404, bad input -> 400) and the MQTT-unavailable 503 guard on
the control endpoints. Upstream/DB calls are monkeypatched -- no network or
InfluxDB is touched."""
import pytest

import airnow
import app as app_module
import away
import google_aq
import home_config
import influx
import purpleair


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_latest_no_data_is_404(client, monkeypatch):
    monkeypatch.setattr(influx, "query_latest", lambda: None)
    assert client.get("/api/latest").status_code == 404


def test_latest_influx_error_is_502(client, monkeypatch):
    def boom():
        raise RuntimeError("influx down")
    monkeypatch.setattr(influx, "query_latest", boom)
    assert client.get("/api/latest").status_code == 502


def test_latest_ok(client, monkeypatch):
    monkeypatch.setattr(influx, "query_latest", lambda: {"aqi": 5, "time": "t"})
    res = client.get("/api/latest")
    assert res.status_code == 200
    assert res.get_json()["aqi"] == 5


def test_history_influx_error_is_502(client, monkeypatch):
    def boom(_hours):
        raise RuntimeError("influx down")
    monkeypatch.setattr(influx, "query_history", boom)
    assert client.get("/api/history?hours=24").status_code == 502


def test_forecast_bad_zip_is_400(client):
    assert client.get("/api/forecast?zip=abc").status_code == 400


def test_forecast_unconfigured_provider_is_400(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_AQ_API_KEY", raising=False)
    res = client.get("/api/forecast?zip=54501&provider=google")
    assert res.status_code == 400
    assert "configured" in res.get_json()["error"]


def test_forecast_purpleair_is_rejected_not_silently_airnow(client):
    res = client.get("/api/forecast?zip=54501&provider=purpleair")
    assert res.status_code == 400
    assert "no forecast" in res.get_json()["error"].lower()


def test_control_503_when_mqtt_unavailable(client, monkeypatch):
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: False)
    assert client.post("/api/control/button/esp_reboot").status_code == 503


def test_control_unknown_switch_is_404(client, monkeypatch):
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    res = client.post("/api/control/switch/nope", json={"state": True})
    assert res.status_code == 404


def test_control_led_alarm_mode_is_accepted(client, monkeypatch):
    """The mode toggle must be in the switch allowlist. Before this it held only
    prevent_sleep, so the control would have 404'd rather than reaching the
    device."""
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    published = []
    monkeypatch.setattr(
        app_module.mqtt_bridge, "publish_switch",
        lambda object_id, on: published.append((object_id, on)),
    )
    res = client.post("/api/control/switch/led_alarm_mode", json={"state": True})
    assert res.status_code == 200
    assert published == [("led_alarm_mode", True)]


def test_control_switch_rejects_non_boolean_state(client, monkeypatch):
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    res = client.post("/api/control/switch/led_alarm_mode", json={"state": "on"})
    assert res.status_code == 400


def test_led_brightness_accepts_full_range_including_zero(client, monkeypatch):
    """0 is the off position, so the bound has to start there -- upstream's
    number floors at 5, and reusing that would leave no way to turn the LED
    off from the dashboard."""
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    published = []
    monkeypatch.setattr(
        app_module.mqtt_bridge, "publish_number",
        lambda object_id, value: published.append((object_id, value)),
    )
    for value in (0, 50, 100):
        res = client.post(
            "/api/control/number/led_brightness", json={"value": value}
        )
        assert res.status_code == 200, value
    assert published == [
        ("led_brightness", 0),
        ("led_brightness", 50),
        ("led_brightness", 100),
    ]


def test_led_brightness_rejects_out_of_range(client, monkeypatch):
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    res = client.post(
        "/api/control/number/led_brightness", json={"value": 101}
    )
    assert res.status_code == 400


def test_control_number_out_of_bounds_is_400(client, monkeypatch):
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    res = client.post("/api/control/number/sleep_duration", json={"value": 99999})
    assert res.status_code == 400


def test_control_number_rejects_non_number(client, monkeypatch):
    monkeypatch.setattr(app_module.mqtt_bridge, "available", lambda: True)
    res = client.post("/api/control/number/sleep_duration", json={"value": "abc"})
    assert res.status_code == 400


# ---------- mode=away (see away.py / home_config.py) ----------

def test_outside_away_no_location_is_404(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away", lambda: None)
    res = client.get("/api/outside?provider=google&mode=away")
    assert res.status_code == 404
    assert "no away location set" in res.get_json()["error"]


def test_outside_away_unsupported_provider_rejected(client):
    # Only providers away.py actually supports are offered in Away mode.
    res = client.get("/api/outside?provider=nope&mode=away")
    assert res.status_code == 400
    assert "Away mode" in res.get_json()["error"]


def test_outside_away_airnow_uses_live_current_not_history(client, monkeypatch):
    # AirNow's *historical* endpoint (the chart) collapses each hour to one
    # dominant AQI with no per-pollutant breakdown -- current conditions calls
    # the live current-observation endpoint directly instead (same one Home's
    # AirNow reading uses), so it shows a full pollutant breakdown like every
    # other provider rather than a bare headline number.
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    monkeypatch.setattr(away.airnow, "get_current_observation", lambda zip_code: {
        "aqi": 172, "category": "Unhealthy", "band": "bad", "dominant_pollutant": "O3",
        "reporting_area": "Chicago, IL", "observed_hour": 14,
        "pollutants": [{"parameter": "O3", "aqi": 172, "category": "Unhealthy"}],
    })
    res = client.get("/api/outside?provider=airnow&mode=away")
    assert res.status_code == 200
    body = res.get_json()
    assert body["aqi"] == 172
    assert body["pollutants"] == [{"parameter": "O3", "aqi": 172, "category": "Unhealthy"}]


def test_outside_away_airnow_no_data_is_404(client, monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    monkeypatch.setattr(away.airnow, "get_current_observation", lambda zip_code: None)
    res = client.get("/api/outside?provider=airnow&mode=away")
    assert res.status_code == 404


def test_outside_away_purpleair_no_sensor_has_specific_reason(client, monkeypatch):
    # A dim provider chip alone doesn't say why -- this is the message that
    # ends up as its hover title and on the Outside card when you switch to
    # it, so it should name the actual cause, not a generic "no data".
    monkeypatch.setenv("PURPLEAIR_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    monkeypatch.setattr(away.purpleair, "get_away_history", lambda lat, lon, days: {"points": [], "sensor": None})
    away._cache = away.aq_shared.TTLCache(away.CACHE_TTL_S)
    res = client.get("/api/outside?provider=purpleair&mode=away")
    assert res.status_code == 404
    assert res.get_json()["error"] == "no healthy PurpleAir sensor nearby"


def test_outside_away_missing_key_is_400(client, monkeypatch):
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    monkeypatch.delenv("GOOGLE_AQ_API_KEY", raising=False)
    res = client.get("/api/outside?provider=google&mode=away")
    assert res.status_code == 400
    assert "configured" in res.get_json()["error"]


def test_outside_away_happy_path(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    monkeypatch.setattr(away, "current", lambda provider, loc: {
        "aqi": 42, "band": "good", "category": "Good", "dominant_pollutant": "PM2.5",
        "reporting_area": loc["reporting_area"], "observed_hour": None, "time": "t", "pollutants": [],
    })
    res = client.get("/api/outside?provider=google&mode=away")
    assert res.status_code == 200
    assert res.get_json()["reporting_area"] == "Chicago"


def test_outside_all_away_covers_every_provider(client, monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setenv("OWM_API_KEY", "k")
    monkeypatch.setenv("PURPLEAIR_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away", lambda: None)
    res = client.get("/api/outside/all?mode=away")
    assert res.status_code == 200
    body = res.get_json()
    assert set(body.keys()) == {"airnow", "google", "purpleair", "openweathermap"}
    assert all(v == {"available": False, "reason": "no away location set"} for v in body.values())


def test_outside_all_fetches_providers_in_parallel(client, monkeypatch):
    # A hung/slow provider (confirmed live: AirNow returning a 504 after its
    # full 10s timeout) must not block the others from appearing -- total
    # time should be bounded by the slowest single provider, not their sum.
    import time

    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setenv("OWM_API_KEY", "k")
    monkeypatch.setenv("PURPLEAIR_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})

    def slow_airnow(zip_code):
        time.sleep(0.3)
        return None

    monkeypatch.setattr(away.airnow, "get_current_observation", slow_airnow)
    monkeypatch.setattr(away.google_aq, "get_away_history", lambda lat, lon, days: [])
    monkeypatch.setattr(away.owm, "get_away_history", lambda lat, lon, days: [])
    monkeypatch.setattr(away.purpleair, "get_away_history", lambda lat, lon, days: {"points": [], "sensor": None})
    away._cache = away.aq_shared.TTLCache(away.CACHE_TTL_S)

    start = time.monotonic()
    res = client.get("/api/outside/all?mode=away")
    elapsed = time.monotonic() - start

    assert res.status_code == 200
    assert elapsed < 0.6  # well under 4x0.3s -- would be ~1.2s+ if still sequential


def test_outside_all_gives_up_on_a_provider_past_the_budget(client, monkeypatch):
    # A provider that's still running past _OUTSIDE_ALL_TIMEOUT_S is reported
    # unavailable rather than making the whole chip row wait on it (that
    # thread keeps running on its own in the background -- not asserted here,
    # just that this request doesn't block on it).
    import threading
    import time

    monkeypatch.setattr(app_module, "_OUTSIDE_ALL_TIMEOUT_S", 0.1)
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setenv("OWM_API_KEY", "k")
    monkeypatch.setenv("PURPLEAIR_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})

    release = threading.Event()

    def hung_airnow(zip_code):
        release.wait(timeout=2)  # released at the end of the test, not the request
        return None

    monkeypatch.setattr(away.airnow, "get_current_observation", hung_airnow)
    monkeypatch.setattr(away.google_aq, "get_away_history", lambda lat, lon, days: [])
    monkeypatch.setattr(away.owm, "get_away_history", lambda lat, lon, days: [])
    monkeypatch.setattr(away.purpleair, "get_away_history", lambda lat, lon, days: {"points": [], "sensor": None})
    away._cache = away.aq_shared.TTLCache(away.CACHE_TTL_S)

    start = time.monotonic()
    try:
        res = client.get("/api/outside/all?mode=away")
        elapsed = time.monotonic() - start
        assert res.status_code == 200
        assert elapsed < 1  # bounded by the 0.1s budget, not airnow's 2s hang
        body = res.get_json()
        assert body["airnow"] == {"available": False, "reason": "taking too long to respond"}
    finally:
        release.set()  # let the background thread finish so it doesn't linger past the test


def test_outside_history_away_trims_to_hours(client, monkeypatch):
    from datetime import UTC, datetime, timedelta
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    now = datetime.now(UTC)
    old = (now - timedelta(days=5)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(away, "history", lambda provider, loc, days, force=False: {
        "points": [{"time": old, "aqi": 1}, {"time": recent, "aqi": 2}],
    })
    res = client.get("/api/outside/history?provider=google&mode=away&hours=24")
    assert res.status_code == 200
    points = res.get_json()
    assert points == [{"time": recent, "aqi": 2}]


def test_outside_away_repeated_requests_share_one_upstream_call(client, monkeypatch):
    # A page refresh (or the current-conditions card and the history chart
    # both loading) must not re-hit the provider -- away.history's 1h TTL
    # cache is shared across every mode=away route, keyed regardless of
    # which endpoint asks first.
    # The real .env is loaded into os.environ at app import time -- explicitly
    # unset the other providers' keys so a mistake here can't quietly fall
    # through to a real network call the way it did until this was caught.
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    monkeypatch.delenv("OWM_API_KEY", raising=False)
    monkeypatch.delenv("PURPLEAIR_API_KEY", raising=False)
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    calls = []
    monkeypatch.setattr(away.google_aq, "get_away_history", lambda lat, lon, days: (
        calls.append(1) or [{"time": "2026-07-20T10:00:00Z", "aqi": 42, "pm2_5_ugm3": 12.0}]
    ))
    away._cache = away.aq_shared.TTLCache(away.CACHE_TTL_S)

    assert client.get("/api/outside?provider=google&mode=away").status_code == 200
    assert client.get("/api/outside?provider=google&mode=away").status_code == 200
    assert client.get("/api/outside/history?provider=google&mode=away&hours=24").status_code == 200
    assert client.get("/api/outside/all?mode=away").status_code == 200

    assert len(calls) == 1  # every request above served from the one cache entry


def test_forecast_zip_resolves_away_location(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    monkeypatch.setattr(home_config, "get_away",
                        lambda: {"zip": "60601", "lat": 41.8, "lon": -87.6, "reporting_area": "Chicago"})
    seen = {}

    # get_forecast itself is captured by reference into app.py's
    # _LIVE_FORECAST_PROVIDERS at import time, so patching it here wouldn't
    # reach that call site -- patch the worker it calls through its cache
    # instead (same as the away-history tests patch each provider's private
    # HTTP call rather than its public wrapper).
    def fake_fetch_forecast(lat, lon):
        seen["lat"], seen["lon"] = lat, lon
        return {"days": []}

    monkeypatch.setattr(google_aq, "_fetch_forecast", fake_fetch_forecast)
    res = client.get("/api/forecast?zip=60601&provider=google")
    assert res.status_code == 200
    assert seen == {"lat": 41.8, "lon": -87.6}
    assert res.get_json()["reporting_area"] == "Chicago"


def test_forecast_home_zip_reads_stored_coords_not_a_live_airnow_call(client, monkeypatch):
    # _resolve_home_latlon used to re-resolve home's coordinates via a fresh
    # live AirNow zip lookup on every call -- confirmed that can drift several
    # km from what's actually stored (two live lookups for the same zip
    # landed ~11km apart). It now just reads the saved home record.
    monkeypatch.setenv("GOOGLE_AQ_API_KEY", "k")
    calls = []
    monkeypatch.setattr(airnow, "get_current_observation", lambda zip_code: calls.append(zip_code) or None)
    seen = {}

    def fake_fetch_forecast(lat, lon):
        seen["lat"], seen["lon"] = lat, lon
        return {"days": []}

    monkeypatch.setattr(google_aq, "_fetch_forecast", fake_fetch_forecast)
    expected_home = home_config.get_home()
    res = client.get("/api/forecast?provider=google")
    assert res.status_code == 200
    assert calls == []  # no live AirNow call for home's own coordinates
    assert seen["lat"] == expected_home["lat"]
    assert seen["lon"] == expected_home["lon"]


def test_home_set_uses_resolved_coordinates_by_default(client, monkeypatch):
    monkeypatch.setattr(airnow, "get_current_observation",
                        lambda zip_code: {"lat": 42.5, "lon": -83.4, "reporting_area": "Somewhere, MI"})
    monkeypatch.setattr(purpleair, "nearest_outdoor_sensor", lambda lat, lon: None)
    monkeypatch.setattr(home_config, "set_home", lambda **kw: (kw, True))
    res = client.post("/api/home", json={"zip": "48324"})
    assert res.status_code == 200
    home = res.get_json()["home"]
    assert home["lat"] == 42.5 and home["lon"] == -83.4


def test_home_set_coordinate_override(client, monkeypatch):
    monkeypatch.setattr(airnow, "get_current_observation",
                        lambda zip_code: {"lat": 42.5, "lon": -83.4, "reporting_area": "Somewhere, MI"})
    seen = {}
    monkeypatch.setattr(purpleair, "nearest_outdoor_sensor",
                        lambda lat, lon: seen.update(lat=lat, lon=lon) or None)
    monkeypatch.setattr(home_config, "set_home", lambda **kw: (kw, True))
    res = client.post("/api/home", json={"zip": "48324", "lat": 42.5988, "lon": -83.3577})
    assert res.status_code == 200
    home = res.get_json()["home"]
    assert home["lat"] == 42.5988 and home["lon"] == -83.3577
    # The PurpleAir search used the override, not the zip's resolved point.
    assert seen == {"lat": 42.5988, "lon": -83.3577}


def test_home_set_invalid_coordinates_is_400(client, monkeypatch):
    monkeypatch.setattr(airnow, "get_current_observation",
                        lambda zip_code: {"lat": 42.5, "lon": -83.4, "reporting_area": "Somewhere, MI"})
    res = client.post("/api/home", json={"zip": "48324", "lat": "nope", "lon": -83.4})
    assert res.status_code == 400
    assert "numbers" in res.get_json()["error"]


def test_home_set_out_of_range_coordinates_is_400(client, monkeypatch):
    monkeypatch.setattr(airnow, "get_current_observation",
                        lambda zip_code: {"lat": 42.5, "lon": -83.4, "reporting_area": "Somewhere, MI"})
    res = client.post("/api/home", json={"zip": "48324", "lat": 999, "lon": -83.4})
    assert res.status_code == 400
    assert "range" in res.get_json()["error"]
