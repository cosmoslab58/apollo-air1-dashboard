"""Home + Away location -- the single source of truth for which place the
dashboard tracks.

- **Home** drives Node-RED's polling and is logged to InfluxDB. When it changes
  the app pushes the home record to Node-RED over a *retained* MQTT config topic
  (see mqtt_bridge.publish_config); Node-RED reads coords/zip/sensor from it and
  tags every `outside_air_quality` write with the home `location_slug`. Coords
  and tag therefore always change together, from this one record.
- **Away** is a live, unpersisted read-through the app renders from provider
  historical APIs (see away.py). Its selection is stored here so the view
  survives a refresh, but it is *never* published to Node-RED and never logged.

Stored as one JSON file on the shared data volume, same read-modify-write +
atomic-replace pattern as locations.py."""

import json
import os
import re
import threading

import mqtt_bridge

DATA_DIR = os.environ.get("DATA_DIR", "data")
HOME_FILE = os.path.join(DATA_DIR, "home.json")

# Retained topic Node-RED subscribes to for the home poll config. Overridable so
# a test/staging deploy -- or another tenant -- can point at its own topic.
# The default must stay inside this tenant's own root: Node-RED subscribes to
# the wildcard `+/air1/config/home` and takes the FIRST topic segment as the
# tenant id, so a topic outside that shape lands under the wrong tenant (or,
# outside the broker ACL grant, is silently denied at QoS 0).
CONFIG_TOPIC = os.environ.get("HOME_CONFIG_TOPIC", "cosmoslab/air1/config/home")

ZIP_RE = re.compile(r"^\d{5}$")

# Serializes read-modify-write, same reasoning as locations.py: gunicorn runs
# one worker but many threads, so two concurrent set_* calls would otherwise
# lost-update each other despite the atomic replace.
_write_lock = threading.Lock()


def _default_home():
    # The pre-existing home this dashboard was built around, kept in sync with
    # Node-RED's built-in fallback literals (Orchard Lake coords, PurpleAir
    # 178257 "St. Marys", slug orchard_lake -- the slug Node-RED already tags
    # writes with). Shown until the user saves a home through the app, so the
    # display reflects what's actually being polled rather than a hollow blank.
    # This default is never auto-published (republish_home only fires for a
    # genuinely saved home.json), so it changes nothing about what Node-RED does.
    return {
        "zip": os.environ.get("AIRNOW_ZIP", "48324"),
        "lat": 42.5988,
        "lon": -83.3577,
        "location_slug": "orchard_lake",
        "reporting_area": "Orchard Lake, MI",
        "purpleair_sensor": 178257,
    }


def _load():
    if not os.path.exists(HOME_FILE):
        return {"home": _default_home(), "away": None}
    with open(HOME_FILE) as f:
        data = json.load(f)
    # Tolerate a partial/older file: always present home+away keys.
    data.setdefault("home", _default_home())
    data.setdefault("away", None)
    return data


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HOME_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, HOME_FILE)


def slugify(text, fallback):
    """A stable, lower-snake location slug for the Influx `location` tag. Falls
    back to `fallback` (e.g. "zip_48324") when the text has no usable words, so
    the tag is never empty."""
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return slug or fallback


def get():
    return _load()


def get_home():
    return _load()["home"]


def get_away():
    return _load()["away"]


def home_zip():
    """The active home zip, for the many callers that only need that (falls
    back to the AIRNOW_ZIP env when no home has been saved yet)."""
    return get_home().get("zip") or os.environ.get("AIRNOW_ZIP", "")


def set_home(*, zip_code, lat, lon, reporting_area, purpleair_sensor, label=None):
    """Persist a new home and push it to Node-RED (retained). The slug is
    derived once here from the friendly label (or AirNow's reporting area, or
    the zip) so the Influx tag is human-readable and stable. Returns
    (home, published) where `published` is False if the broker link was down --
    the file is still written, and Node-RED will pick it up from the retained
    topic once the app reconnects and re-publishes, or on the next save."""
    slug = slugify(label or reporting_area, f"zip_{zip_code}")
    home = {
        "zip": zip_code,
        "lat": lat,
        "lon": lon,
        "location_slug": slug,
        "reporting_area": reporting_area,
        "purpleair_sensor": purpleair_sensor,
    }
    with _write_lock:
        data = _load()
        data["home"] = home
        _save(data)
    published = mqtt_bridge.publish_config(CONFIG_TOPIC, {
        "zip": zip_code,
        "lat": lat,
        "lon": lon,
        "location_slug": slug,
        "purpleair_sensor": purpleair_sensor,
    })
    return home, published


def republish_home():
    """Re-send the stored home to Node-RED. Called at app boot so a home saved
    in a previous run is (re)published as a retained message even if the broker
    was down at the moment it was originally saved. No-op/False when the link
    is down or no real home (with coords) has been saved yet."""
    # Only a genuinely saved home should be re-pushed -- never the seeded
    # default (which matches Node-RED's own literals anyway), so a fresh app
    # with no home.json never publishes anything Node-RED wasn't already doing.
    if not os.path.exists(HOME_FILE):
        return False
    home = get_home()
    if home.get("lat") is None:
        return False
    return mqtt_bridge.publish_config(CONFIG_TOPIC, {
        "zip": home.get("zip"),
        "lat": home.get("lat"),
        "lon": home.get("lon"),
        "location_slug": home.get("location_slug"),
        "purpleair_sensor": home.get("purpleair_sensor"),
    })


def set_away(*, zip_code, lat, lon, reporting_area):
    """Persist the away selection. Deliberately does NOT publish to Node-RED or
    touch InfluxDB -- away is a live, unpersisted view. No PurpleAir sensor is
    stored: away.get_away_history resolves the nearest sensor live each time."""
    away = {
        "zip": zip_code,
        "lat": lat,
        "lon": lon,
        "reporting_area": reporting_area,
    }
    with _write_lock:
        data = _load()
        data["away"] = away
        _save(data)
    return away


def clear_away():
    with _write_lock:
        data = _load()
        data["away"] = None
        _save(data)
