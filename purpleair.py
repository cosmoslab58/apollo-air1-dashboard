"""PurpleAir provider -- hyperlocal PM readings from the nearest outdoor
community sensor. Unlike AirNow/Google there's no regional model here: it's
one physical sensor, so readings are minutes fresh but PM-only (no gases).

automation's air1 tab polls the home's sensor every 10 minutes and writes
corrected PM2.5/PM10 + a recomputed AQI to InfluxDB (format_purpleair in
automation/tabs/air1/logic.py); this module just reads that
back, same pattern as owm.py. The dashboard itself never calls PurpleAir.
Raw cf_1 PM2.5 is corrected there with EPA's Barkjohn equation (the same
one AirNow's own Fire & Smoke map applies to PurpleAir sensors) before
converting to an AQI, so this provider's number is comparable to AirNow's
rather than reading systematically high."""

import math
import os
from datetime import UTC, datetime

import requests

import aq_shared
import epa_aqi
import influx

# The nearest outdoor sensor to home, picked once (sensor 178257). It is also
# the fallback sensor in automation's air1 tab (build_purpleair) when the home
# record carries none. If that sensor ever goes offline, both need a manual update.
SENSOR_NAME = "St. Marys"

# (concentration field, EPA parameter, units, per-pollutant AQI field). The AQI
# is the one automation's air1 tab derived from the EPA-corrected concentration; the app
# reads it for the dashboard while the Technical page keeps the concentration.
_CONCENTRATION_FIELDS = [
    ("purpleair_pm2_5_ugm3", "PM2.5", "MICROGRAMS_PER_CUBIC_METER", "purpleair_pm2_5_aqi_epa"),
    ("purpleair_pm10_ugm3", "PM10", "MICROGRAMS_PER_CUBIC_METER", "purpleair_pm10_aqi_epa"),
]


def get_current_observation():
    """Headline AQI, category, and dominant pollutant are read straight from
    the fields automation's air1 tab computed (purpleair_aqi_epa / purpleair_category /
    purpleair_dominant_pollutant) from the EPA-corrected PM concentrations --
    the app no longer recomputes them, so the dashboard, Grafana, and
    automation all agree on the number. Concentrations pass through raw (already
    the corrected values), so no extra rounding here."""
    return aq_shared.observation(
        influx.query_purpleair_latest(),
        aqi_field="purpleair_aqi_epa",
        category_field="purpleair_category",
        dominant_field="purpleair_dominant_pollutant",
        concentration_fields=_CONCENTRATION_FIELDS,
        reporting_area=SENSOR_NAME,
        round_concentration=False,
    )


def get_history(hours):
    """Reads back what automation's air1 tab has been persisting to InfluxDB (see
    get_current_observation above) rather than PurpleAir's own history
    endpoint -- that endpoint is gated by API key tier and often just isn't
    available, so this is the only reliable source of PurpleAir history,
    not just a consistency choice. Field names are stripped back to the
    shared flat shape (aqi/pm2_5_ugm3/pm10_ugm3) the frontend's overlay
    charts already expect, same convention as owm.py/google_aq.py's
    get_history."""
    field_map = {
        "pm2_5_ugm3": ("purpleair_pm2_5_ugm3", aq_shared.identity),
        "pm10_ugm3": ("purpleair_pm10_ugm3", aq_shared.identity),
    }
    return aq_shared.history_points(influx.query_purpleair_history(hours), "purpleair_aqi_epa", field_map)


# ----------------------------------------------------------------------------
# Live PurpleAir calls -- used ONLY for the Away view and for resolving the
# nearest sensor when the user sets a location. The Home path never calls
# PurpleAir directly (automation's air1 tab polls it into InfluxDB, read back above).
# ----------------------------------------------------------------------------

PA_API_BASE = "https://api.purpleair.com/v1"

# Half-width of the lat/lon box searched around a location (~0.15° ≈ 12-16 km),
# wide enough that even a semi-rural home usually has a community sensor inside.
_SENSOR_SEARCH_RADIUS_DEG = 0.15
_SENSOR_MAX_AGE_S = 3600         # ignore sensors that haven't reported in > 1h
_SENSOR_MIN_CONFIDENCE = 90      # PurpleAir's 0-100 data-quality score


def corrected_pm25(pa_cf1, humidity):
    """EPA's US-wide Barkjohn correction for PurpleAir's raw cf_1 PM2.5 -- the
    same correction AirNow's Fire & Smoke map applies, and the one automation's
    air1 tab applies on the Home path (corrected_pm25 in automation/shared/aq.py).
    Replicated here because the Away path can't lean on automation. Humidity defaults to 50% when the sensor
    didn't report it."""
    if pa_cf1 is None:
        return None
    rh = humidity if humidity is not None else 50
    if pa_cf1 > 343:
        corrected = 0.46 * pa_cf1 + 3.93e-4 * pa_cf1 ** 2 + 2.97
    else:
        corrected = 0.52 * pa_cf1 - 0.086 * rh + 5.75
    return max(0.0, corrected)


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_outdoor_sensor(lat, lon):
    """Return the nearest *healthy* outdoor PurpleAir sensor to (lat, lon), or
    None. Queries a bounding box (PurpleAir has no point-nearest query), then
    filters to outdoor / recently-seen / high-confidence / no A-B channel
    disagreement (channel_flags == 0) before ranking by distance -- a closer
    but flaky sensor is worse than a solid one slightly further out. Result:
    {index, name, distance_km, confidence, last_seen}. Returns None with no
    API key set so callers degrade to "no PurpleAir for this location"."""
    key = os.environ.get("PURPLEAIR_API_KEY")
    if not key:
        return None
    r = _SENSOR_SEARCH_RADIUS_DEG
    params = {
        "fields": "name,latitude,longitude,confidence,channel_flags,last_seen",
        "location_type": 0,  # outdoor only
        "max_age": _SENSOR_MAX_AGE_S,
        "nwlng": lon - r, "nwlat": lat + r,
        "selng": lon + r, "selat": lat - r,
    }
    resp = requests.get(f"{PA_API_BASE}/sensors", params=params,
                        headers={"X-API-Key": key}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    fields = body.get("fields") or []

    best = None
    for row in body.get("data") or []:
        rec = dict(zip(fields, row, strict=False))
        flags = rec.get("channel_flags")
        if flags is not None and flags != 0:  # A/B disagreement or downgrade
            continue
        confidence = rec.get("confidence")
        if confidence is not None and confidence < _SENSOR_MIN_CONFIDENCE:
            continue
        slat, slon = rec.get("latitude"), rec.get("longitude")
        if slat is None or slon is None:
            continue
        dist = _haversine_km(lat, lon, slat, slon)
        if best is None or dist < best["distance_km"]:
            best = {
                "index": rec.get("sensor_index"),
                "name": rec.get("name"),
                "distance_km": round(dist, 2),
                "confidence": confidence,
                "last_seen": rec.get("last_seen"),
            }
    return best


def get_away_history(lat, lon, days):
    """7-ish day PurpleAir history for an away location: find the nearest
    healthy sensor, pull its hourly history, apply the Barkjohn correction and
    EPA AQI locally, and reshape into the shared flat history-point shape the
    overlay charts already read. Returns {points, sensor} -- sensor is None
    (and points empty) when no suitable sensor is nearby."""
    sensor = nearest_outdoor_sensor(lat, lon)
    if sensor is None or sensor.get("index") is None:
        return {"points": [], "sensor": None}

    key = os.environ.get("PURPLEAIR_API_KEY")
    start = int(datetime.now(UTC).timestamp()) - days * 86400
    params = {
        "fields": "pm2.5_cf_1,pm10.0_atm,humidity",
        "start_timestamp": start,
        "average": 60,  # hourly averages
    }
    resp = requests.get(f"{PA_API_BASE}/sensors/{sensor['index']}/history",
                        params=params, headers={"X-API-Key": key}, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    fields = body.get("fields") or []

    points = []
    for row in body.get("data") or []:
        rec = dict(zip(fields, row, strict=False))
        ts = rec.get("time_stamp")
        if ts is None:
            continue
        pm25 = corrected_pm25(rec.get("pm2.5_cf_1"), rec.get("humidity"))
        pm10 = rec.get("pm10.0_atm")
        aqi = _worst_pm_aqi(pm25, pm10)
        point = {"time": datetime.fromtimestamp(ts, UTC).isoformat(), "aqi": aqi}
        if pm25 is not None:
            point["pm2_5_ugm3"] = round(pm25, 1)
        if pm10 is not None:
            point["pm10_ugm3"] = round(pm10, 1)
        points.append(point)

    points.sort(key=lambda p: p["time"])
    return {"points": points, "sensor": sensor}


def _worst_pm_aqi(pm25, pm10):
    candidates = [
        epa_aqi.aqi_from_concentration("PM2.5", pm25),
        epa_aqi.aqi_from_concentration("PM10", pm10),
    ]
    candidates = [a for a in candidates if a is not None]
    return max(candidates) if candidates else None
