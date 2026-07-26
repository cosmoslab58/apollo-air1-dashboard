import concurrent.futures
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

import airnow
import aq_shared
import away
import google_aq
import home_config
import influx
import mqtt_bridge
import owm
import purpleair

load_dotenv()

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

PORT = int(os.environ.get("PORT", 5858))
HOST = os.environ.get("HOST", "0.0.0.0")
DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "t")

NUMBER_BOUNDS = {
    mqtt_bridge.SLEEP_DURATION: (0, 800),
    mqtt_bridge.SEN55_TEMPERATURE_OFFSET: (-70, 70),
    mqtt_bridge.SEN55_HUMIDITY_OFFSET: (-70, 70),
    mqtt_bridge.DPS310_PRESSURE_OFFSET: (-100, 100),
    # 0 is the off position, matching the firmware's widened floor.
    mqtt_bridge.LED_BRIGHTNESS: (0, 100),
}
SWITCH_IDS = {
    mqtt_bridge.PREVENT_SLEEP,
    mqtt_bridge.LED_ALARM_MODE,
}
BUTTON_IDS = {
    mqtt_bridge.CALIBRATE_SCD40,
    mqtt_bridge.CLEAN_SEN55,
    mqtt_bridge.ESP_REBOOT,
    mqtt_bridge.FACTORY_RESET,
}

# Flask's debug reloader re-execs the process in a child with WERKZEUG_RUN_MAIN
# set; only start the MQTT client in the process that's actually serving.
if not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    mqtt_bridge.start()
    # Best-effort re-push of a previously saved home to Node-RED. The broker
    # normally still holds the retained copy from the last save, so this only
    # matters after a broker data loss; it's a no-op when the link isn't up yet
    # (connect is async) or no real home has been saved.
    home_config.republish_home()


@app.context_processor
def inject_links():
    # Optional external link to the Grafana provider-comparison dashboard,
    # shown in the page footer on every page. Unset -> link is omitted.
    return {"grafana_url": os.environ.get("GRAFANA_DASHBOARD_URL", "")}


@app.route("/healthz")
def healthz():
    # Liveness only -- deliberately does NOT touch InfluxDB or MQTT, so an
    # upstream outage doesn't make the container look dead and get killed. It
    # reports the MQTT link state as a hint without gating health on it.
    return jsonify({"status": "ok", "mqtt_connected": mqtt_bridge.available()})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/forecast")
def forecast_page():
    return render_template("forecast.html")


@app.route("/technical")
def technical_page():
    return render_template("technical.html")


@app.route("/indoor")
def indoor_page():
    return render_template("indoor.html")


@app.route("/sw.js")
def service_worker():
    # Served from the root (not /static/sw.js) so its default scope covers the whole app.
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")


@app.route("/api/latest")
def api_latest():
    try:
        row = influx.query_latest()
    except Exception:
        logging.exception("Failed to query latest reading from InfluxDB")
        return jsonify({"error": "influxdb query failed"}), 502
    if row is None:
        return jsonify({"error": "no data yet"}), 404
    return jsonify(row)


@app.route("/api/history")
def api_history():
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    hours = max(1, min(hours, 168))
    try:
        points = influx.query_history(hours)
    except Exception:
        logging.exception("Failed to query history from InfluxDB")
        return jsonify({"error": "influxdb query failed"}), 502
    return jsonify(points)


def _resolve_home_latlon():
    """Google's/OWM's forecast APIs need coordinates, not a zip -- read them
    straight from the saved home record instead of re-resolving via a fresh
    AirNow zip lookup each time. A second live call isn't guaranteed to
    return the same point as the one used when home was saved: AirNow
    resolves a zip to its *nearest reporting station*, not a true centroid,
    and that can drift several km between calls (confirmed directly -- two
    live lookups for the same zip landed ~11km apart, enough to flip which
    PurpleAir sensor came back nearest). Reading the stored value keeps every
    consumer of "home's coordinates" pinned to the one point that was
    actually resolved (or manually entered) at save time -- see api_home_set."""
    home = home_config.get_home()
    if home.get("lat") is None:
        return None, None, None
    return home["lat"], home["lon"], home.get("reporting_area") or "Home"


def _home_reporting_area():
    """The home reporting-area label for provider cards that don't carry
    their own (Google/OpenWeatherMap). Read from AirNow's stored reading in
    InfluxDB rather than a live AirNow call, so a current-conditions view
    never depends on hitting an upstream API. Falls back to "Home" if AirNow
    hasn't been polled into the DB yet."""
    try:
        row = influx.query_airnow_latest()
    except Exception:
        logging.exception("Failed to read AirNow home label from InfluxDB")
        return "Home"
    if row and row.get("reporting_area"):
        return row["reporting_area"]
    return "Home"


def _resolve_latlon_for_zip(zip_code):
    if zip_code == home_config.home_zip():
        return _resolve_home_latlon()
    # The only other zip Forecast's ?zip= mechanism is ever asked to resolve
    # is the Away location -- the saved-locations list this used to also fall
    # back to was removed along with Forecast's own location picker.
    away_loc = home_config.get_away()
    if away_loc and away_loc.get("zip") == zip_code and away_loc.get("lat") is not None:
        return away_loc["lat"], away_loc["lon"], away_loc.get("reporting_area")
    return None, None, None


def _read_from_db(fetch, label, missing_msg):
    """Run a provider's DB read and map it to (data, error, status): a query
    exception -> 502, no data -> 404 with the provider's own hint, otherwise
    (data, None, 200). Collapses the identical boilerplate every provider's
    current-conditions read used to repeat."""
    try:
        data = fetch()
    except Exception:
        logging.exception("Failed to read %s from InfluxDB", label)
        return None, "influxdb query failed", 502
    if data is None:
        return None, missing_msg, 404
    return data, None, 200


# provider -> (fetch, log label, 404 hint). All read current conditions back
# from InfluxDB (Node-RED polls every provider in); AirNow is the default.
_CURRENT_READERS = {
    "google": (google_aq.get_current_observation, "Google air quality",
               "no Google air quality data yet — is GOOGLE_AQ_API_KEY set on the nodered container?"),
    "purpleair": (purpleair.get_current_observation, "PurpleAir",
                  "no PurpleAir data yet — is PURPLEAIR_API_KEY set on the nodered container?"),
    "openweathermap": (owm.get_current_observation, "OpenWeatherMap pollution",
                       "no OpenWeatherMap pollution data yet — is OWM_API_KEY set on the nodered container?"),
    "airnow": (lambda: airnow.observation_from_row(influx.query_airnow_latest()), "AirNow",
               "no AirNow data yet — is AIRNOW_API_KEY set on the nodered container?"),
}
# Providers whose reading carries no reporting-area of its own -> stamp the
# shared home label (from AirNow's stored reading).
_NEEDS_HOME_LABEL = {"google", "openweathermap"}


def _fetch_away_current(provider):
    """Away's analogue of _read_from_db above: live per-provider read via
    away.current() instead of an InfluxDB query. Only providers away.py
    supports (see away.PROVIDER_KEYS) are offered -- anything else fails
    closed with a 400 rather than silently serving Home."""
    env_key = away.PROVIDER_KEYS.get(provider)
    if env_key is None:
        return None, f"{provider} isn't available in Away mode", 400
    if not os.environ.get(env_key):
        return None, f"{provider} isn't configured", 400
    away_loc = home_config.get_away()
    if not away_loc or away_loc.get("lat") is None:
        return None, "no away location set", 404
    try:
        data = away.current(provider, away_loc)
    except Exception:
        logging.exception("Away current-conditions fetch failed provider=%s", provider)
        return None, f"{provider} request failed", 502
    if data is None:
        # PurpleAir's "no data" case is specifically "no healthy sensor found
        # near this location" (see purpleair.nearest_outdoor_sensor) -- worth
        # saying plainly rather than the generic message, since it's the one
        # the dashboard's provider chip and Outside card surface directly.
        reason = "no healthy PurpleAir sensor nearby" if provider == "purpleair" else "no data available for this away location"
        return None, reason, 404
    return data, None, 200


def _fetch_outside_current(provider, *, want_discussion=True, home_label=None, mode="home"):
    """Returns (data, error_message, http_status). data is None on error.

    want_discussion=False skips the live AirNow forecast-discussion fetch, for
    callers (the /api/outside/all chip summary) that don't render it.
    home_label lets a caller resolve the shared home label once and pass it in
    rather than each provider re-querying it. mode="away" reads live from the
    Away location instead (see _fetch_away_current) -- discussion/home_label
    don't apply there."""
    if mode == "away":
        return _fetch_away_current(provider)

    fetch, label, missing_msg = _CURRENT_READERS.get(provider, _CURRENT_READERS["airnow"])
    data, error, status = _read_from_db(fetch, label, missing_msg)
    if data is None:
        return data, error, status

    if provider in _NEEDS_HOME_LABEL:
        data["reporting_area"] = home_label if home_label is not None else _home_reporting_area()

    if want_discussion and provider == "airnow":
        # AirNow's forecaster discussion only exists on the forecast endpoint,
        # not current conditions, and the forecast isn't persisted -- reuse
        # get_forecast's own cache (already hit by the Forecast page, the one
        # sanctioned live AirNow call) rather than a separate lookup. Best-
        # effort: a forecast hiccup shouldn't take down the current reading.
        try:
            forecast = airnow.get_forecast(home_config.home_zip())
            data["discussion"] = forecast.get("discussion") if forecast else None
        except Exception:
            logging.exception("Failed to fetch forecast discussion from AirNow")
            data["discussion"] = None
    return data, None, 200


@app.route("/api/outside")
def api_outside():
    provider = request.args.get("provider", "airnow")
    mode = request.args.get("mode", "home")
    data, error, status = _fetch_outside_current(provider, mode=mode)
    if data is None:
        return jsonify({"error": error}), status
    return jsonify(data)


_OUTSIDE_PROVIDERS = ("airnow", "google", "purpleair", "openweathermap")


# This is a glance summary for the provider chips, not a considered read of
# any one of them -- a provider that hasn't answered within this budget is
# reported unavailable rather than making the whole chip row wait on it.
# Confirmed directly against a real AirNow outage: its own HTTP timeout is
# 10s, well past what a "glance" should ever wait for.
_OUTSIDE_ALL_TIMEOUT_S = 5


@app.route("/api/outside/all")
def api_outside_all():
    """Compact current summary from every provider at once, for the
    at-a-glance provider chips. Each is best-effort and served from the
    same caches the full endpoint uses, so this costs no extra upstream
    calls beyond what browsing the providers individually would.

    Fetched in parallel, not one after another, and bounded by
    _OUTSIDE_ALL_TIMEOUT_S -- these are independent calls (each provider's
    own API or DB query), and running them sequentially with no budget meant
    one slow/hung provider (confirmed directly: AirNow's API returning a 504
    only after its full 10s timeout) blocked every other chip from appearing
    at all, not just its own. A provider that's still running past the
    budget keeps running in its own thread (so it still populates that
    module's cache whenever it does finish, same as before) -- this request
    just stops waiting on it."""
    mode = request.args.get("mode", "home")
    # Resolve the shared home label once rather than per-provider, and skip the
    # AirNow discussion fetch entirely -- the chips only show aqi/band/category.
    home_label = _home_reporting_area() if mode != "away" else None

    def fetch_one(provider):
        data, error, _ = _fetch_outside_current(provider, want_discussion=False, home_label=home_label, mode=mode)
        return provider, data, error

    summary = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(_OUTSIDE_PROVIDERS))
    try:
        future_to_provider = {pool.submit(fetch_one, provider): provider for provider in _OUTSIDE_PROVIDERS}
        done, not_done = concurrent.futures.wait(future_to_provider, timeout=_OUTSIDE_ALL_TIMEOUT_S)
        for future in done:
            provider, data, error = future.result()
            if data is None:
                summary[provider] = {"available": False, "reason": error}
            else:
                summary[provider] = {
                    "available": True,
                    "aqi": data.get("aqi"),
                    "band": data.get("band"),
                    "category": data.get("category"),
                    "dominant_pollutant": data.get("dominant_pollutant"),
                }
        for future in not_done:
            summary[future_to_provider[future]] = {"available": False, "reason": "taking too long to respond"}
    finally:
        # wait=False -- don't block the response on threads we've already
        # given up on; Python can't force-kill a thread mid requests.get()
        # anyway, so it finishes (or times out) on its own in the background.
        pool.shutdown(wait=False)
    return jsonify(summary)


def _with_outside_weather(points, hours):
    """Outside temperature/humidity/pressure comes from neither AirNow nor
    Google (neither API provides weather) -- it's a separate feed the user
    writes into the same InfluxDB measurement. Merged in as extra points
    (not matched to existing timestamps) since the frontend's per-field
    lookup only cares that a point has the field, not which point. Best-
    effort: a user who hasn't wired that feed up yet, or a query hiccup,
    should still get pollutant history back.
    """
    try:
        return points + influx.query_outside_weather(hours)
    except Exception:
        logging.exception("Failed to fetch outside weather from InfluxDB")
        return points


# provider -> (history reader, config env var or None, config label, log label).
# All read pollutant history back from InfluxDB; the per-pollutant temp/
# humidity/pressure always comes from the shared outside-weather feed merged in
# by _with_outside_weather (PurpleAir/OWM/Google don't persist their own).
# AirNow is the default and reads the bare (unprefixed) fields directly.
_HISTORY_READERS = {
    "google": (google_aq.get_history, "GOOGLE_AQ_API_KEY", "Google Air Quality",
               "Failed to read Google history from InfluxDB"),
    "purpleair": (purpleair.get_history, None, None,
                  "Failed to read PurpleAir history from InfluxDB"),
    "openweathermap": (owm.get_history, None, None,
                       "Failed to read OpenWeatherMap history from InfluxDB"),
}


def _api_outside_history_away(provider, hours):
    """Away's analogue of the DB-backed branch below: one cached 7-day live
    fetch (see away.history) trimmed to the requested window with
    aq_shared.points_since, so every range the Technical page's toggle offers
    (up to 7d) reuses the same cache entry instead of a separate upstream call
    per range. The shared outside-weather feed (_with_outside_weather) is
    Home-only -- it's physically tied to Home's own sensor, so it's skipped."""
    env_key = away.PROVIDER_KEYS.get(provider)
    if env_key is None:
        return jsonify({"error": f"{provider} isn't available in Away mode"}), 400
    if not os.environ.get(env_key):
        return jsonify({"error": f"{provider} isn't configured"}), 400
    away_loc = home_config.get_away()
    if not away_loc or away_loc.get("lat") is None:
        return jsonify({"error": "no away location set"}), 404
    try:
        result = away.history(provider, away_loc, days=7)
    except Exception:
        logging.exception("Away history fetch failed provider=%s", provider)
        return jsonify({"error": f"{provider} request failed"}), 502
    points = (result or {}).get("points") or []
    return jsonify(aq_shared.points_since(points, hours))


@app.route("/api/outside/history")
def api_outside_history():
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    hours = max(1, min(hours, 168))
    provider = request.args.get("provider", "airnow")
    mode = request.args.get("mode", "home")

    if mode == "away":
        return _api_outside_history_away(provider, hours)

    reader = _HISTORY_READERS.get(provider)
    if reader is not None:
        fetch, env_key, cfg_label, log_label = reader
        if env_key and not os.environ.get(env_key):
            return jsonify({"error": f"{cfg_label} isn't configured"}), 400
    else:
        fetch, log_label = influx.query_outside_history, "Failed to query outdoor history from InfluxDB"

    try:
        points = fetch(hours)
    except Exception:
        logging.exception(log_label)
        return jsonify({"error": "influxdb query failed"}), 502
    return jsonify(_with_outside_weather(points, hours))


# provider -> (live forecast fetch(lat, lon, force), config env var, config
# label, upstream-failure message). These are the two on-demand live forecast
# providers -- both need coordinates (not a zip) and both stamp the resolved
# home/location label onto the response. AirNow (the default) is handled
# separately below: it forecasts by zip and carries its own reporting area.
_LIVE_FORECAST_PROVIDERS = {
    "google": (google_aq.get_forecast, "GOOGLE_AQ_API_KEY", "Google Air Quality",
               "google air quality request failed"),
    "openweathermap": (owm.get_forecast, "OWM_API_KEY", "OpenWeatherMap",
                       "openweathermap request failed"),
}


@app.route("/api/forecast")
def api_forecast():
    zip_code = request.args.get("zip") or home_config.home_zip()
    if not home_config.ZIP_RE.match(zip_code):
        return jsonify({"error": "zip must be 5 digits"}), 400
    provider = request.args.get("provider", "airnow")
    force = request.args.get("refresh") in ("1", "true")

    if provider == "purpleair":
        # One real-time sensor, no forward-looking model -- there is no
        # PurpleAir forecast to serve. Reject rather than silently falling
        # through to AirNow below, which would show the wrong agency's data
        # under a "PurpleAir" label.
        return jsonify({"error": "PurpleAir has no forecast"}), 400

    live = _LIVE_FORECAST_PROVIDERS.get(provider)
    if live is not None:
        fetch, env_key, cfg_label, fail_msg = live
        if not os.environ.get(env_key):
            return jsonify({"error": f"{cfg_label} isn't configured"}), 400
        lat, lon, label = _resolve_latlon_for_zip(zip_code)
        if lat is None:
            return jsonify({"error": "couldn't resolve coordinates for that zip"}), 502
        try:
            data = fetch(lat, lon, force=force)
        except Exception:
            logging.exception("Failed to fetch forecast from %s", cfg_label)
            return jsonify({"error": fail_msg}), 502
        if data is None:
            return jsonify({"error": "no forecast for this location"}), 404
        data["reporting_area"] = label
        data["provider"] = provider
        return jsonify(data)

    try:
        data = airnow.get_forecast(zip_code, force=force)
    except Exception:
        logging.exception("Failed to fetch forecast from AirNow")
        return jsonify({"error": "airnow request failed"}), 502
    if data is None:
        return jsonify({"error": "no forecast for this location"}), 404
    data["provider"] = "airnow"
    return jsonify(data)


def _resolve_zip(zip_code):
    """Resolve a zip to coordinates + reporting area via AirNow's current-obs
    response (which geocodes the zip for us), and double as a coverage check --
    a zip AirNow can't resolve won't have data for any provider. Returns a dict
    or None."""
    try:
        obs = airnow.get_current_observation(zip_code)
    except Exception:
        logging.exception("Failed to resolve zip via AirNow zip=%s", zip_code)
        return None
    if not obs or obs.get("lat") is None:
        return None
    return {"lat": obs["lat"], "lon": obs["lon"], "reporting_area": obs.get("reporting_area")}


@app.route("/api/home")
def api_home_get():
    return jsonify(home_config.get_home())


@app.route("/api/home", methods=["POST"])
def api_home_set():
    """Change the tracked home location. Resolves + verifies the zip, resolves
    the nearest healthy PurpleAir sensor, then persists and pushes the record
    to Node-RED over the retained config topic. Changing home starts a fresh
    Influx series under the new location slug -- old data stays under the old
    one."""
    body = request.get_json(silent=True) or {}
    zip_code = (body.get("zip") or "").strip()
    label = (body.get("label") or "").strip() or None
    if not home_config.ZIP_RE.match(zip_code):
        return jsonify({"error": "zip must be 5 digits"}), 400
    resolved = _resolve_zip(zip_code)
    if resolved is None:
        return jsonify({"error": "couldn't resolve or verify that zip with AirNow"}), 400

    # The zip is still required -- AirNow's own Home poll (via Node-RED) only
    # takes a zip, and it doubles as the reporting-area label lookup above.
    # But AirNow's zip resolution is "nearest reporting station", not a true
    # centroid, and can land noticeably off from the actual location (see
    # _resolve_home_latlon) -- optional exact coordinates override that
    # point for everything else that matters (the PurpleAir sensor search
    # right below, and Google/OWM's forecast).
    lat, lon = resolved["lat"], resolved["lon"]
    lat_in, lon_in = body.get("lat"), body.get("lon")
    if lat_in is not None and lon_in is not None:
        try:
            lat, lon = float(lat_in), float(lon_in)
        except (TypeError, ValueError):
            return jsonify({"error": "coordinates must be numbers"}), 400
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify({"error": "coordinates out of range"}), 400

    # An explicit sensor index in the body is honored if ever passed (e.g. a
    # future admin/scripted call); the app's own UI never sends one anymore,
    # so this always auto-resolves, best-effort (no nearby sensor -> None).
    sensor = None
    sensor_index = body.get("purpleair_sensor")
    if sensor_index is None:
        try:
            sensor = purpleair.nearest_outdoor_sensor(lat, lon)
        except Exception:
            logging.exception("PurpleAir nearest-sensor lookup failed")
        sensor_index = sensor["index"] if sensor else None

    home, published = home_config.set_home(
        zip_code=zip_code, lat=lat, lon=lon,
        reporting_area=resolved["reporting_area"], purpleair_sensor=sensor_index, label=label,
    )
    return jsonify({"home": home, "purpleair": sensor, "published": published})


@app.route("/api/away")
def api_away_get():
    return jsonify(home_config.get_away())


@app.route("/api/away", methods=["POST"])
def api_away_set():
    body = request.get_json(silent=True) or {}
    zip_code = (body.get("zip") or "").strip()
    if not home_config.ZIP_RE.match(zip_code):
        return jsonify({"error": "zip must be 5 digits"}), 400
    resolved = _resolve_zip(zip_code)
    if resolved is None:
        return jsonify({"error": "couldn't resolve that zip with AirNow"}), 400
    away_loc = home_config.set_away(
        zip_code=zip_code, lat=resolved["lat"], lon=resolved["lon"],
        reporting_area=resolved["reporting_area"],
    )
    return jsonify(away_loc)


@app.route("/api/away", methods=["DELETE"])
def api_away_clear():
    home_config.clear_away()
    return jsonify({"cleared": True})


@app.route("/api/controls")
def api_controls():
    return jsonify(mqtt_bridge.get_state())


def _mqtt_unavailable_response():
    """Shared 503 for the mutating control routes when the command cannot land.

    Two ways it can't. The broker may be unreachable from here (client never
    came up, or the link dropped) -- checking that also keeps these routes from
    dereferencing a None client and 500ing with an AttributeError.

    Or the broker is fine and the AIR-1 is the one that's away. That case used
    to return a hollow 200: commands go out QoS 0 and non-retained to a topic
    only the device subscribes to, so one published while it is disconnected is
    dropped at the broker and never replayed. The control would flip in the UI,
    toast "Sent", and silently snap back on the next poll or refresh -- which
    is exactly what a WiFi/broker dropout looked like from the sofa. Refusing
    is the honest answer: nothing was sent, and nothing will be."""
    if not mqtt_bridge.available():
        return jsonify({"error": "device control is unavailable — MQTT broker not connected"}), 503
    if not mqtt_bridge.device_online():
        return jsonify({"error": "the AIR-1 isn't connected right now — nothing was sent"}), 503
    return None


@app.route("/api/control/switch/<object_id>", methods=["POST"])
def api_control_switch(object_id):
    if object_id not in SWITCH_IDS:
        return jsonify({"error": "unknown switch"}), 404
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("state"), bool):
        return jsonify({"error": "state must be a boolean"}), 400
    # Deliberately after validation: a bad object_id or payload is a bug in the
    # caller and says so whether or not the device happens to be reachable.
    unavailable = _mqtt_unavailable_response()
    if unavailable:
        return unavailable
    mqtt_bridge.publish_switch(object_id, body["state"])
    return jsonify({"published": True})


@app.route("/api/control/number/<object_id>", methods=["POST"])
def api_control_number(object_id):
    bounds = NUMBER_BOUNDS.get(object_id)
    if bounds is None:
        return jsonify({"error": "unknown number"}), 404
    body = request.get_json(silent=True) or {}
    value = body.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return jsonify({"error": "value must be a number"}), 400
    lo, hi = bounds
    if not (lo <= value <= hi):
        return jsonify({"error": f"value must be between {lo} and {hi}"}), 400
    unavailable = _mqtt_unavailable_response()
    if unavailable:
        return unavailable
    mqtt_bridge.publish_number(object_id, value)
    return jsonify({"published": True})


@app.route("/api/control/button/<object_id>", methods=["POST"])
def api_control_button(object_id):
    if object_id not in BUTTON_IDS:
        return jsonify({"error": "unknown button"}), 404
    unavailable = _mqtt_unavailable_response()
    if unavailable:
        return unavailable
    mqtt_bridge.publish_button(object_id)
    return jsonify({"published": True})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
