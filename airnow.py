import os
import re
from datetime import UTC, datetime, timedelta

import requests

import aq_shared
from epa_aqi import band_for_aqi, band_for_category  # used internally; band math lives in epa_aqi

# AirNow retired its zipCode/latLong services on 2026-10-01 (they now answer with a
# WebServiceError). These are the replacements; their payloads use lowerCamelCase keys,
# and the observation ones carry no state code, no coordinates and no category number.
API_URL = "https://www.airnowapi.org/aq/observation/current/ziplatlong/"
HISTORICAL_API_URL = "https://www.airnowapi.org/aq/observation/historical/state/"
FORECAST_API_URL = "https://www.airnowapi.org/aq/forecast/current/"
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


# US AQS monitor ids are SSCCCNNNN with SS the state FIPS code: the only place the new
# current-observation payload still says which state a reading is in.
_FIPS_STATES = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}
# Category names on AirNow's 1-6 scale, for band_for_category when no AQI was computed.
_CATEGORY_NUMBERS = {
    "good": 1, "moderate": 2, "unhealthy for sensitive groups": 3,
    "unhealthy": 4, "very unhealthy": 5, "hazardous": 6,
}


def _state_from_site(site_id):
    site_id = str(site_id or "")
    return _FIPS_STATES.get(site_id[:2]) if re.fullmatch(r"\d{9}", site_id) else None


def _category_number(name):
    return _CATEGORY_NUMBERS.get((name or "").strip().lower())


def _hour(value):
    """hourObserved arrives as "18:00" now (an int before); the UI wants the int."""
    try:
        return int(str(value).split(":")[0])
    except (TypeError, ValueError):
        return None


def _get(url, params):
    resp = requests.get(url, params={"format": "application/json", **params,
                                     "API_KEY": os.environ["AIRNOW_API_KEY"]}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("WebServiceError"):
        # AirNow answers errors (a retired service, a bad key) with HTTP 200 and this object;
        # treating it as "no data" is how the 2026-10-01 retirement went unnoticed for a month.
        raise RuntimeError(f"AirNow {url}: {data['WebServiceError'][0].get('Message')}")
    return data


_cache = aq_shared.TTLCache(CACHE_TTL_S)  # current-conditions, keyed by zip
_forecast_cache = aq_shared.TTLCache(FORECAST_CACHE_TTL_S)  # forecast, keyed by zip


def _fetch(zip_code):
    readings = _get(API_URL, {"zipCode": zip_code, "distance": SEARCH_DISTANCE_MI})
    if not readings:
        return None

    # AQI is -1 on readings AirNow hasn't computed a number for -- same
    # sentinel the forecast endpoint uses (see _fetch_forecast). Rank by
    # category first so an uncomputed row never outranks a real one,
    # and surface the AQI as None rather than passing -1 through, which
    # band_for_aqi would otherwise read as a healthy "good".
    def aqi_of(r):
        a = r.get("nowcastAQI")
        return a if isinstance(a, (int, float)) and a > 0 else None

    dominant = max(readings, key=lambda r: (_category_number(r.get("aqiCategoryName")) or 0,
                                            r.get("nowcastAQI") or -1))
    dominant_aqi = aqi_of(dominant)
    area = dominant.get("reportingAreaName") or ""
    state = _state_from_site(dominant.get("siteID")) or _forecast_state(zip_code)
    return {
        "aqi": dominant_aqi,
        "category": dominant.get("aqiCategoryName"),
        "band": band_for_aqi(dominant_aqi) if dominant_aqi
        else band_for_category(_category_number(dominant.get("aqiCategoryName"))),
        "dominant_pollutant": _parameter(dominant.get("parameterName")),
        "reporting_area": _format_reporting_area(area, state) if state else area,
        # The raw pair, which the historical-by-state service is keyed on.
        "reporting_area_name": area,
        "state_code": state,
        "observed_hour": _hour(dominant.get("hourObserved")),
        "pollutants": [
            {
                "parameter": _parameter(r.get("parameterName")),
                "aqi": aqi_of(r),
                "category": r.get("aqiCategoryName"),
            }
            for r in readings
        ],
    }


def _forecast_state(zip_code):
    """Some reporting areas come back with no siteID; the forecast service, which
    is cached for hours and already called for the Forecast page, still names the
    state. Best-effort."""
    try:
        forecast = get_forecast(zip_code)
    except Exception:
        return None
    return forecast.get("state_code") if forecast else None


def _parameter(name):
    """The new services say "OZONE" where the old ones (and the rest of the app) say "O3"."""
    return "O3" if (name or "").upper() == "OZONE" else name


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
    rows = _get(FORECAST_API_URL, {"zipCode": zip_code, "distance": SEARCH_DISTANCE_MI})
    if not rows:
        return None

    by_date = {}
    discussion = None
    for r in rows:
        by_date.setdefault(r["dateValid"], []).append(r)
        if not discussion and (r.get("discussion") or "").strip():
            discussion = _clean_discussion(r["discussion"])

    days = []
    for date in sorted(by_date):
        readings = by_date[date]
        # AQI is -1 on rows AirNow hasn't computed a number for (common on
        # active alert days) — categoryNumber is still meaningful then, so
        # rank by that first and use it as the band fallback.
        dominant = max(readings, key=lambda r: (r.get("categoryNumber") or 0, r.get("aqi") or -1))
        dominant_aqi = dominant["aqi"] if dominant.get("aqi") and dominant["aqi"] > 0 else None
        days.append({
            "date": date,
            "aqi": dominant_aqi,
            "category": dominant.get("categoryName"),
            "band": band_for_aqi(dominant_aqi) if dominant_aqi else band_for_category(dominant.get("categoryNumber")),
            "dominant_pollutant": _parameter(dominant.get("parameterName")),
            # An agency-issued "Action Day" designation -- distinct from the
            # AQI number itself, e.g. PM2.5 can be flagged even on a day its
            # own AQI wasn't computed (see the -1/category fallback above).
            "action_day": any(r.get("actionDay") for r in readings),
            "pollutants": [
                {
                    "parameter": _parameter(r.get("parameterName")),
                    "aqi": r["aqi"] if r.get("aqi") and r["aqi"] > 0 else None,
                    "category": r.get("categoryName"),
                }
                for r in readings
            ],
        })

    return {
        "reporting_area": _format_reporting_area(rows[0].get("reportingArea"), rows[0].get("stateCode")),
        "state_code": rows[0].get("stateCode"),
        "discussion": discussion,
        "days": days,
        # Stamped once here (not by the TTL cache) so it stays correct across
        # cache hits -- it's when this forecast was actually fetched, not when
        # the page happened to ask for it.
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def get_forecast(zip_code, force=False):
    return _forecast_cache.get(zip_code, lambda: _fetch_forecast(zip_code), force=force)


def get_away_history(zip_code, days):
    """LIVE AirNow daily history for an away location. The zip-based historical
    service was retired; its replacement is keyed by state and returns daily
    values for every reporting area in it over a date range, so: one
    current-observation lookup (cached) for the zip's reporting area and state,
    one call for the window, then that area's dominant (max across pollutants)
    daily AQI. Points come back in the shared flat history shape ({time, aqi}),
    one per day as before. Best-effort: any failure is an empty series."""
    try:
        obs = get_current_observation(zip_code)
        if not obs or not obs.get("state_code") or not obs.get("reporting_area_name"):
            return []
        today = datetime.now(UTC).date()
        rows = _get(HISTORICAL_API_URL, {
            "stateCode": obs["state_code"],
            "startDate": (today - timedelta(days=days - 1)).isoformat(),
            "endDate": today.isoformat(),
        }) or []
    except Exception:
        return []
    area = obs["reporting_area_name"].strip().lower()
    by_day = {}
    for r in rows:
        if (r.get("reportingAreaName") or "").strip().lower() != area:
            continue
        aqi = r.get("dailyAQI")
        day = (r.get("dateObserved") or "").strip()
        if aqi is None or aqi < 0 or not day:  # -1 = not computed
            continue
        by_day[day] = max(by_day.get(day, 0), int(round(aqi)))
    return [{"time": f"{d}T00:00:00", "aqi": v} for d, v in sorted(by_day.items())]
