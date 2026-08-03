import os
import re
from datetime import UTC, datetime, timedelta

import requests

import aq_shared
from epa_aqi import band_for_aqi, band_for_category  # used internally; band math lives in epa_aqi

API_URL = "https://www.airnowapi.org/aq/observation/zipCode/current/"
HISTORICAL_API_URL = "https://www.airnowapi.org/aq/observation/zipCode/historical/"
FORECAST_API_URL = "https://www.airnowapi.org/aq/forecast/zipCode/"
CACHE_TTL_S = 20 * 60  # AirNow refreshes hourly, but the exact publish moment within the
# hour isn't fixed and its own reporting lag varies -- a shorter cache
# means a fetch that lands just before AirNow publishes a new hour
# doesn't sit stale for most of the following hour too.
FORECAST_CACHE_TTL_S = 3 * 60 * 60  # forecasts are issued at most a couple times a day

# AirNow resolves a zip to its nearest reporting area within this radius.
# Urban zips typically have one within 25mi, but rural zips (e.g. northern
# Wisconsin) can be 50-100mi from the nearest station — 200 is generous
# enough to cover those without changing which station wins for zips that
# already resolve at a shorter distance (it's always the *nearest* one).
SEARCH_DISTANCE_MI = 200

US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
# AirNow sometimes reports a compass-direction region instead of a city (e.g.
# "Southeast" for the southeast Michigan reporting area) -- "Southeast, MI"
# reads like a made-up town name, so those get the state's full name folded
# in ("Southeast Michigan") instead of the usual "City, ST" abbreviation.
_REGIONAL_REPORTING_AREA_WORDS = {
    "north", "south", "east", "west",
    "northeast", "northwest", "southeast", "southwest",
    "central", "metro",
}


def _format_reporting_area(area, state_code):
    if not area:
        return state_code or ""
    if area.strip().lower() in _REGIONAL_REPORTING_AREA_WORDS:
        return f"{area} {US_STATE_NAMES.get(state_code, state_code)}".strip()
    return f"{area}, {state_code}"


_cache = aq_shared.TTLCache(CACHE_TTL_S)  # current-conditions, keyed by zip
_forecast_cache = aq_shared.TTLCache(FORECAST_CACHE_TTL_S)  # forecast, keyed by zip


def _fetch(zip_code):
    resp = requests.get(
        API_URL,
        params={
            "format": "application/json",
            "zipCode": zip_code,
            "distance": SEARCH_DISTANCE_MI,
            "API_KEY": os.environ["AIRNOW_API_KEY"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    readings = resp.json()
    if not readings:
        return None

    # AQI is -1 on readings AirNow hasn't computed a number for -- same
    # sentinel the forecast endpoint uses (see _fetch_forecast). Rank by
    # Category.Number first so an uncomputed row never outranks a real one,
    # and surface the AQI as None rather than passing -1 through, which
    # band_for_aqi would otherwise read as a healthy "good".
    dominant = max(readings, key=lambda r: (r["Category"]["Number"], r["AQI"]))
    dominant_aqi = dominant["AQI"] if dominant["AQI"] and dominant["AQI"] > 0 else None
    return {
        "aqi": dominant_aqi,
        "category": dominant["Category"]["Name"],
        "band": band_for_aqi(dominant_aqi) if dominant_aqi else band_for_category(dominant["Category"]["Number"]),
        "dominant_pollutant": dominant["ParameterName"],
        "reporting_area": _format_reporting_area(dominant["ReportingArea"], dominant["StateCode"]),
        "lat": dominant.get("Latitude"),
        "lon": dominant.get("Longitude"),
        "observed_hour": dominant["HourObserved"],
        "pollutants": [
            {
                "parameter": r["ParameterName"],
                "aqi": r["AQI"] if r["AQI"] and r["AQI"] > 0 else None,
                "category": r["Category"]["Name"],
            }
            for r in readings
        ],
    }


def get_current_observation(zip_code):
    return _cache.get(zip_code, lambda: _fetch(zip_code))


# Node-RED writes each AirNow parameter's AQI to a <param>_aqi field, keyed by
# the same lower-cased/underscored ParameterName it reports (see the "Format
# AirNow Fields & Tags" function) -- mapped back to display labels here.
_ROW_POLLUTANT_FIELDS = [
    ("pm2_5_aqi", "PM2.5"), ("pm10_aqi", "PM10"), ("o3_aqi", "O3"),
    ("no2_aqi", "NO2"), ("co_aqi", "CO"), ("so2_aqi", "SO2"),
]


_DISCUSSION_HEADING = re.compile(r"^\s*FORECAST DISCUSSION\s*:\s*", re.IGNORECASE)


def _clean_discussion(text):
    """AirNow's Discussion field arrives with its own "FORECAST DISCUSSION:"
    heading and CRLF line endings. The heading duplicates the disclosure the UI
    already puts above the text, and a stray \\r renders as a blank line under
    the CSS's white-space: pre-line -- so both go here, once, rather than in
    each of the two page scripts that display this."""
    return _DISCUSSION_HEADING.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def observation_from_row(row):
    """Build the same current-observation shape _fetch returns, but from a
    stored InfluxDB row (influx.query_airnow_latest) instead of a live AirNow
    call -- current conditions are polled into the DB by Node-RED, so the
    dashboard reads them back like every other provider. The forecast, which
    carries the forecaster discussion and isn't persisted, stays live."""
    if row is None:
        return None
    aqi = row.get("aqi")
    if aqi is None:
        return None
    aqi = int(round(aqi))
    pollutants = []
    for field, parameter in _ROW_POLLUTANT_FIELDS:
        value = row.get(field)
        if value is not None:
            pollutants.append({"parameter": parameter, "aqi": int(round(value)), "category": None})
    return {
        "aqi": aqi,
        "category": row.get("category"),
        "band": band_for_aqi(aqi),
        "dominant_pollutant": row.get("dominant_pollutant"),
        "reporting_area": row.get("reporting_area"),
        "observed_hour": None,
        "time": row.get("time"),
        "pollutants": pollutants,
    }


def _fetch_forecast(zip_code):
    resp = requests.get(
        FORECAST_API_URL,
        params={
            "format": "application/json",
            "zipCode": zip_code,
            "distance": SEARCH_DISTANCE_MI,
            "API_KEY": os.environ["AIRNOW_API_KEY"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None

    by_date = {}
    discussion = None
    for r in rows:
        by_date.setdefault(r["DateForecast"], []).append(r)
        if not discussion and (r.get("Discussion") or "").strip():
            discussion = _clean_discussion(r["Discussion"])

    days = []
    for date in sorted(by_date):
        readings = by_date[date]
        # AQI is -1 on rows AirNow hasn't computed a number for (common on
        # active alert days) — Category.Number is still meaningful then, so
        # rank by that first and use it as the band fallback.
        dominant = max(readings, key=lambda r: (r["Category"]["Number"], r["AQI"]))
        dominant_aqi = dominant["AQI"] if dominant["AQI"] and dominant["AQI"] > 0 else None
        days.append({
            "date": date,
            "aqi": dominant_aqi,
            "category": dominant["Category"]["Name"],
            "band": band_for_aqi(dominant_aqi) if dominant_aqi else band_for_category(dominant["Category"]["Number"]),
            "dominant_pollutant": dominant["ParameterName"],
            # An agency-issued "Action Day" designation -- distinct from the
            # AQI number itself, e.g. PM2.5 can be flagged even on a day its
            # own AQI wasn't computed (see the -1/Category fallback above).
            "action_day": any(r.get("ActionDay") for r in readings),
            "pollutants": [
                {
                    "parameter": r["ParameterName"],
                    "aqi": r["AQI"] if r["AQI"] and r["AQI"] > 0 else None,
                    "category": r["Category"]["Name"],
                }
                for r in readings
            ],
        })

    return {
        "reporting_area": _format_reporting_area(rows[0]["ReportingArea"], rows[0]["StateCode"]),
        "lat": rows[0].get("Latitude"),
        "lon": rows[0].get("Longitude"),
        "discussion": discussion,
        "days": days,
        # Stamped once here (not by the TTL cache) so it stays correct across
        # cache hits -- it's when this forecast was actually fetched, not when
        # the page happened to ask for it.
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def get_forecast(zip_code, force=False):
    return _forecast_cache.get(zip_code, lambda: _fetch_forecast(zip_code), force=force)


def _fetch_historical(zip_code, day):
    """One AirNow historical-observation call for a single date. Best-effort:
    returns [] on any error so a sparse/failed day just contributes no points
    rather than sinking the whole window."""
    try:
        resp = requests.get(
            HISTORICAL_API_URL,
            params={
                "format": "application/json",
                "zipCode": zip_code,
                "date": day.strftime("%Y-%m-%dT00-0000"),
                "distance": SEARCH_DISTANCE_MI,
                "API_KEY": os.environ["AIRNOW_API_KEY"],
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json() or []
    except Exception:
        return []


def get_away_history(zip_code, days):
    """LIVE AirNow historical-by-zip for an away location. AirNow's historical
    endpoint is date-parameterized (one call per day) and its archive is sparse,
    so this is a coarser series than the hourly providers -- one call per day
    over the window, keeping the dominant (max across pollutants) AQI at each
    observed hour. Points come back in the shared flat history shape ({time,
    aqi}). AirNow is keyed by zip directly, so no geocoding is needed."""
    today = datetime.now(UTC).date()
    by_time = {}
    for d in range(days):
        day = today - timedelta(days=d)
        for r in _fetch_historical(zip_code, day):
            aqi = r.get("AQI")
            if aqi is None or aqi < 0:  # -1 = not computed
                continue
            date_observed = (r.get("DateObserved") or "").strip()
            hour = r.get("HourObserved")
            if not date_observed or hour is None:
                continue
            ts = f"{date_observed}T{int(hour):02d}:00:00"
            if ts not in by_time or aqi > by_time[ts]:
                by_time[ts] = int(round(aqi))
    return [{"time": t, "aqi": v} for t, v in sorted(by_time.items())]
