"""Small helpers shared by the provider modules (airnow/google_aq/purpleair/
owm), so the current-reading, history, and forecast-caching patterns live in
one place instead of being copy-pasted per provider."""

import time
from datetime import UTC, datetime, timedelta

import epa_aqi


class TTLCache:
    """A tiny time-to-live cache keyed by anything hashable. Used for both the
    short current-conditions cache and the longer forecast cache; `get` fetches
    (and stores) on a miss or when forced."""

    def __init__(self, ttl_s):
        self._ttl = ttl_s
        self._cache = {}

    def get(self, key, fetch, force=False):
        now = time.time()
        cached = self._cache.get(key)
        if not force and cached is not None and (now - cached["at"]) < self._ttl:
            return cached["data"]
        data = fetch()
        self._cache[key] = {"at": now, "data": data}
        return data


def headline(row, aqi_field, category_field=None, dominant_field=None, category_fallback=None):
    """Pull the stored headline (aqi, category, dominant) out of an InfluxDB
    row for a current reading. Returns None if there's no AQI to show. The AQI
    is coerced to an int; category falls back to `category_fallback(aqi)` (e.g.
    epa_aqi.category_name) only when the stored label is missing."""
    aqi = row.get(aqi_field)
    if aqi is None:
        return None
    aqi = int(round(aqi))
    category = row.get(category_field) if category_field else None
    if category is None and category_fallback is not None:
        category = category_fallback(aqi)
    dominant = row.get(dominant_field) if dominant_field else None
    return aqi, category, dominant


def observation(row, *, aqi_field, category_field, dominant_field,
                concentration_fields, reporting_area=None, round_concentration=True):
    """Build the current-observation dict the DB-backed providers
    (google_aq/purpleair/owm) return, from a stored InfluxDB row. Collapses the
    identical shape each used to assemble by hand: headline via `headline()`
    (with epa_aqi.category_name as the category fallback), then a per-pollutant
    row {parameter, concentration_value, concentration_units, aqi?} for each
    present concentration.

    `concentration_fields` is a list of (field, parameter, units, aqi_field)
    tuples. `round_concentration` rounds the stored concentration to 2 dp
    (PurpleAir passes it through raw). Returns None when the row is missing or
    carries no headline AQI. Callers tack on any provider-specific extras (e.g.
    OWM's NH3 concentration row, Google's health block) onto the result."""
    if row is None:
        return None
    hd = headline(row, aqi_field, category_field, dominant_field, epa_aqi.category_name)
    if hd is None:
        return None
    aqi, category, dominant = hd

    pollutants = []
    for field, parameter, units, pollutant_aqi_field in concentration_fields:
        value = row.get(field)
        if value is None:
            continue
        row_out = {
            "parameter": parameter,
            "concentration_value": round(value, 2) if round_concentration else value,
            "concentration_units": units,
        }
        # AQI drives the dashboard; the Technical page keeps the concentration.
        aqi_value = row.get(pollutant_aqi_field)
        if aqi_value is not None:
            row_out["aqi"] = int(round(aqi_value))
        pollutants.append(row_out)

    return {
        "aqi": aqi,
        "band": epa_aqi.band_for_aqi(aqi),
        "category": category,
        "dominant_pollutant": dominant,
        "reporting_area": reporting_area,
        "observed_hour": None,
        "time": row.get("time"),
        "pollutants": pollutants,
    }


def point_to_pollutants(point, field_defs):
    """Build a pollutants list from a flat history point (the shape away.py's
    live fetchers return), for the Away providers that carry no per-pollutant
    AQI in that point -- same concentration-only shape Google/OWM/PurpleAir's
    own DB-backed current-observation cards already show. `field_defs` is
    [(point_key, parameter, units)]."""
    pollutants = []
    for key, parameter, units in field_defs:
        value = point.get(key)
        if value is not None:
            pollutants.append({"parameter": parameter, "concentration_value": value, "concentration_units": units})
    return pollutants


def dominant_from_pollutants(pollutants):
    """Which pollutant drives the reading, by recomputing each one's AQI from
    its concentration and taking the worst -- the same worst-of pattern
    already used by google_aq._recomputed_aqi / owm._forecast_aqi_dominant /
    purpleair._worst_pm_aqi. Concentrations go through epa_aqi.epa_value
    first: the rows carry each provider's own units (ppb gases from Google,
    µg/m³-converted-to-ppb from OWM), and EPA's CO table is in ppm, so
    comparing raw values would hand CO the win on every reading.
    Returns None when no pollutant converts."""
    best = None
    for p in pollutants:
        parameter = p["parameter"]
        value = epa_aqi.epa_value(parameter, p["concentration_value"], p.get("concentration_units"))
        aqi = epa_aqi.aqi_from_concentration(parameter, value)
        if aqi is not None and (best is None or aqi > best[0]):
            best = (aqi, p["parameter"])
    return best[1] if best else None


def away_observation(point, field_defs, reporting_area):
    """Away's analogue of observation() above: assembles the same current-
    conditions shape from one live history point instead of a stored InfluxDB
    row, so every existing renderer (dashboard rack, Technical card) works on
    Away data unmodified. Returns None when the point carries no AQI."""
    aqi = point.get("aqi")
    if aqi is None:
        return None
    pollutants = point_to_pollutants(point, field_defs)
    return {
        "aqi": aqi,
        "band": epa_aqi.band_for_aqi(aqi),
        "category": epa_aqi.category_name(aqi),
        "dominant_pollutant": dominant_from_pollutants(pollutants),
        "reporting_area": reporting_area,
        "observed_hour": None,
        "time": point.get("time"),
        "pollutants": pollutants,
    }


def history_points(rows, aqi_field, field_map):
    """Turn stored InfluxDB rows into the flat history-point shape the
    frontend overlays expect: {time, aqi, <mapped fields>}. `field_map` is
    {out_key: (source_field, transform)} where transform converts the stored
    value (e.g. identity for concentrations, µg/m³->ppb for OWM gases). Points
    with only a timestamp are dropped; floats are rounded to 2 dp."""
    points = []
    for row in rows:
        aqi = row.get(aqi_field)
        point = {"time": row["time"], "aqi": int(round(aqi)) if aqi is not None else None}
        for out_key, (source_field, transform) in field_map.items():
            point[out_key] = transform(row.get(source_field))
        cleaned = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in point.items() if v is not None}
        if len(cleaned) > 1:  # more than just "time"
            points.append(cleaned)
    return points


def points_since(points, hours):
    """Trim a flat point list (see history_points/point_to_pollutants) to the
    trailing `hours` window. Points carry ISO timestamps in whatever format
    the provider gave -- Z-suffixed, a +00:00 offset, or (AirNow's away
    history, built from its DateObserved/HourObserved fields) no offset at
    all. A naive timestamp is assumed UTC rather than left to crash comparing
    against the (aware) cutoff -- an approximation for AirNow specifically,
    whose historical hours are actually station-local, but exact-to-the-hour
    precision isn't the point of an already-daily-granularity chart. Lets one
    cached multi-day fetch (see away.history) serve every range a range
    toggle asks for without a separate upstream call per range."""
    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    def _parse(ts):
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    return [p for p in points if p.get("time") and _parse(p["time"]) >= cutoff]


def worst_hour_per_day(points, date_of, aqi_of):
    """Bucket hourly forecast points into calendar days and pick each day's
    worst hour by AQI -- the shared core of the Google/OWM day forecasts.
    Returns an ordered {date: (aqi, point)} dict; callers build their own
    per-day payload from the winning point."""
    by_date = {}
    for point in points:
        date = date_of(point)
        if date is None:
            continue
        by_date.setdefault(date, []).append(point)
    result = {}
    for date in sorted(by_date):
        best = None
        for point in by_date[date]:
            aqi = aqi_of(point)
            if aqi is not None and (best is None or aqi > best[0]):
                best = (aqi, point)
        if best is not None:
            result[date] = best
    return result


def identity(value):
    return value
