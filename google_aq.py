import os
from datetime import UTC, datetime, timedelta

import requests

import aq_shared
import epa_aqi
import influx

FORECAST_URL = "https://airquality.googleapis.com/v1/forecast:lookup"
HISTORY_URL = "https://airquality.googleapis.com/v1/history:lookup"

FORECAST_CACHE_TTL_S = 3 * 60 * 60
# Google's forecast supports up to 96h out, but rejects a period request
# starting at the exact current instant ("next hour onwards" only) and one
# landing right on the 96h edge ("time period not supported"). Starting at
# the next hour boundary and spanning 71h (72 hourly points, ~3 days) stays
# safely inside both limits -- still well beyond AirNow's ~1-2 day forecast.
FORECAST_SPAN_HOURS = 71

POLLUTANT_CODE_LABELS = {
    "pm25": "PM2.5",
    "pm10": "PM10",
    "o3": "O3",
    "no2": "NO2",
    "so2": "SO2",
    "co": "CO",
}

_forecast_cache = aq_shared.TTLCache(FORECAST_CACHE_TTL_S)  # keyed by (lat, lon)


def _cache_key(lat, lon):
    return (round(lat, 3), round(lon, 3))


def _pollutant_label(code):
    code = (code or "").lower()
    return POLLUTANT_CODE_LABELS.get(code, code.upper())


# Google's own indexes[].aqi/category/dominantPollutant can be inconsistent
# with its own pollutants[] concentrations for the same hour -- confirmed
# directly against the live API: one forecast hour reported usa_epa aqi=55
# ("Moderate"), while that same hour's own PM2.5 concentration (1.82 µg/m³)
# independently computes to single-digit "Good" via EPA's own breakpoint
# math, and its own healthRecommendations text already said "no
# limitations, enjoy the outdoors" -- i.e. two of Google's three fields
# agreed, only the index number was the outlier. Recomputing AQI straight
# from the concentrations (same epa_aqi.py math already used for
# OpenWeatherMap/PurpleAir) guarantees the headline number, category, and
# per-pollutant breakdown are always self-consistent, instead of trusting a
# number that can silently disagree with the data sitting right next to it.
# Node-RED's "Format Google Fields & Tags" function applies this exact same
# math for the current-conditions poll (see the Apollo AIR-1 flow) -- this
# copy is now used only for the still-live Forecast feature below.
def _recomputed_aqi(raw_pollutants):
    best = None
    for p in raw_pollutants:
        parameter = p.get("displayName") or _pollutant_label(p.get("code"))
        concentration = p.get("concentration") or {}
        value = concentration.get("value")
        if value is None:
            continue
        # Google reports gases in ppb and particulates in µg/m³; the shared
        # normalizer maps either onto EPA's own unit for the parameter (ppm
        # for CO, notably).
        aqi = epa_aqi.aqi_from_concentration(
            parameter, epa_aqi.epa_value(parameter, value, concentration.get("units"))
        )
        if aqi is not None and (best is None or aqi > best[0]):
            best = (aqi, parameter)
    if best is None:
        return None, None, None
    aqi, dominant = best
    return aqi, epa_aqi.category_name(aqi), dominant


def _pollutants_from(raw_pollutants):
    # Google's own displayName is already exactly "O3"/"PM2.5"/"NO2" etc --
    # use it verbatim rather than a name we maintain ourselves. Only gives
    # concentration (its own units, which vary by pollutant), never an AQI
    # number -- that's not something Google computes per-pollutant, so we
    # don't fabricate one.
    return [
        {
            "parameter": p.get("displayName") or _pollutant_label(p.get("code")),
            "concentration_value": (p.get("concentration") or {}).get("value"),
            "concentration_units": (p.get("concentration") or {}).get("units"),
        }
        for p in raw_pollutants
    ]


# Google's Air Quality concentration fields, by EPA parameter name, the unit
# Google reports them in (µg/m³ for particulates, ppb for gases), and the
# per-pollutant AQI field Node-RED derives from that concentration -- the app
# reads the AQI rather than computing it.
_CONCENTRATION_FIELDS = [
    ("google_pm2_5_ugm3", "PM2.5", "MICROGRAMS_PER_CUBIC_METER", "google_pm2_5_aqi_epa"),
    ("google_pm10_ugm3", "PM10", "MICROGRAMS_PER_CUBIC_METER", "google_pm10_aqi_epa"),
    ("google_o3_ppb", "O3", "PARTS_PER_BILLION", "google_o3_aqi_epa"),
    ("google_no2_ppb", "NO2", "PARTS_PER_BILLION", "google_no2_aqi_epa"),
    ("google_so2_ppb", "SO2", "PARTS_PER_BILLION", "google_so2_aqi_epa"),
    ("google_co_ppb", "CO", "PARTS_PER_BILLION", "google_co_aqi_epa"),
]


def get_current_observation():
    """Reads back what Node-RED has been polling hourly and writing to
    InfluxDB (see the "Format Google Fields & Tags" function in the Apollo
    AIR-1 flow) instead of calling Google's API directly here -- same
    pattern as owm.py, and the dashboard no longer makes this call itself
    for any provider's current reading. Google's Forecast API is a
    different, on-demand feature and still calls Google live -- see
    get_forecast below.

    The headline AQI, category, and dominant pollutant are read straight
    from the fields Node-RED computed (google_aqi_epa / google_category /
    google_dominant_pollutant), not recomputed from the concentrations --
    same number Grafana reads. Category falls back to a lookup on the AQI
    only if the stored label is somehow absent (e.g. pre-migration points).
    reporting_area is left None for the caller to fill with the home label."""
    row = influx.query_google_latest()
    obs = aq_shared.observation(
        row,
        aqi_field="google_aqi_epa",
        category_field="google_category",
        dominant_field="google_dominant_pollutant",
        concentration_fields=_CONCENTRATION_FIELDS,
    )
    if obs is None:
        return None

    # Only the general-population + children lines are persisted (see the
    # Node-RED flow) -- the per-sensitive-group breakdown isn't stored, so it's
    # simply absent here rather than fabricated. Key is always present (None
    # when Google gave no guidance for this reading).
    obs["health_recommendations"] = None
    if row.get("google_health_general"):
        obs["health_recommendations"] = {
            "generalPopulation": row.get("google_health_general"),
            "children": row.get("google_health_children"),
        }
    return obs


def _fetch_forecast(lat, lon):
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=FORECAST_SPAN_HOURS)
    payload = {
        "location": {"latitude": lat, "longitude": lon},
        "period": {
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "pageSize": 100,
        "extraComputations": ["LOCAL_AQI", "POLLUTANT_CONCENTRATION", "HEALTH_RECOMMENDATIONS"],
        "languageCode": "en",
    }

    hourly = []
    for _ in range(5):  # safety cap against an unbounded pagination loop
        resp = requests.post(FORECAST_URL, params={"key": os.environ["GOOGLE_AQ_API_KEY"]}, json=payload, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        hourly.extend(body.get("hourlyForecasts") or [])
        page_token = body.get("nextPageToken")
        if not page_token:
            break
        payload["pageToken"] = page_token

    if not hourly:
        return None

    # Bucket hourly points into calendar days (UTC) and take the worst hour
    # per day as that day's headline number -- mirrors how AirNow's own
    # per-day forecast already condenses a full day into one reading. The
    # "worst" hour is picked by our own recomputed AQI (_recomputed_aqi),
    # not Google's indexes[].aqi, so the hour selected and the number shown
    # for it are always derived the same way.
    worst = aq_shared.worst_hour_per_day(
        hourly,
        date_of=lambda p: p["dateTime"][:10],
        aqi_of=lambda p: _recomputed_aqi(p.get("pollutants") or [])[0],
    )

    days = []
    for date, (_, point) in worst.items():
        aqi, category, dominant_pollutant = _recomputed_aqi(point.get("pollutants") or [])
        days.append({
            "date": date,
            "aqi": aqi,
            "category": category,
            "band": epa_aqi.band_for_aqi(aqi),
            "dominant_pollutant": dominant_pollutant,
            # Per-population-group guidance for this day's worst hour --
            # more specific than AirNow's one-size-fits-all forecaster
            # discussion, which Google doesn't have an equivalent of.
            "health_recommendations": point.get("healthRecommendations"),
            "pollutants": _pollutants_from(point.get("pollutants") or []),
        })

    return {
        "reporting_area": None,
        "discussion": None,  # Google doesn't provide a forecaster narrative like AirNow's
        "days": days,
        # Stamped once here (not by the TTL cache) so it stays correct across
        # cache hits -- it's when this forecast was actually fetched, not when
        # the page happened to ask for it.
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def get_forecast(lat, lon, force=False):
    return _forecast_cache.get(_cache_key(lat, lon), lambda: _fetch_forecast(lat, lon), force=force)


# Stored concentration field -> flat history key. Google already reports gases
# in ppb, so every mapping is identity; the shared history_points helper reads
# the AQI from google_aqi_epa (not recomputed here).
_HISTORY_FIELD_MAP = {
    "pm2_5_ugm3": ("google_pm2_5_ugm3", aq_shared.identity),
    "pm10_ugm3": ("google_pm10_ugm3", aq_shared.identity),
    "o3_ppb": ("google_o3_ppb", aq_shared.identity),
    "no2_ppb": ("google_no2_ppb", aq_shared.identity),
    "so2_ppb": ("google_so2_ppb", aq_shared.identity),
    "co_ppb": ("google_co_ppb", aq_shared.identity),
}


def get_history(hours):
    """Reads back what Node-RED has been persisting to InfluxDB (see
    get_current_observation above) instead of calling Google's own history
    API -- keeps every provider's History sourced the same way. Field names
    are stripped back to the shared flat shape (aqi/pm2_5_ugm3/o3_ppb/...)
    the frontend's overlay charts already expect, same convention as
    owm.py's get_history."""
    return aq_shared.history_points(influx.query_google_history(hours), "google_aqi_epa", _HISTORY_FIELD_MAP)


# Google history pollutant code -> shared flat history key (gases already ppb,
# particulates µg/m³ -- same convention as _HISTORY_FIELD_MAP above but keyed by
# Google's own pollutant codes, which is what history:lookup returns live).
_AWAY_CODE_FIELD = {
    "pm25": "pm2_5_ugm3", "pm10": "pm10_ugm3", "o3": "o3_ppb",
    "no2": "no2_ppb", "so2": "so2_ppb", "co": "co_ppb",
}


def get_away_history(lat, lon, days):
    """LIVE call to Google's History API for an away location (Home reads the
    DB instead -- see get_history). AQI is recomputed from concentrations with
    the same EPA math as everywhere else. Points come back in the shared flat
    history shape so the away charts reuse the home overlay renderer unchanged.
    Paginated; capped at Google's 720-hour (30-day) limit."""
    hours = min(days * 24, 720)
    payload = {
        "hours": hours,
        "location": {"latitude": lat, "longitude": lon},
        "extraComputations": ["POLLUTANT_CONCENTRATION"],
        "languageCode": "en",
        "pageSize": 168,
    }
    points = []
    for _ in range(8):  # safety cap against an unbounded pagination loop
        resp = requests.post(HISTORY_URL, params={"key": os.environ["GOOGLE_AQ_API_KEY"]},
                             json=payload, timeout=20)
        resp.raise_for_status()
        body = resp.json()
        for hour in body.get("hoursInfo") or []:
            dt = hour.get("dateTime")
            if not dt:
                continue
            pollutants = hour.get("pollutants") or []
            aqi = _recomputed_aqi(pollutants)[0]
            point = {"time": dt, "aqi": aqi}
            for p in pollutants:
                key = _AWAY_CODE_FIELD.get((p.get("code") or "").lower())
                value = (p.get("concentration") or {}).get("value")
                if key and isinstance(value, (int, float)):
                    point[key] = round(value, 2)
            points.append(point)
        token = body.get("nextPageToken")
        if not token:
            break
        payload["pageToken"] = token

    points.sort(key=lambda p: p["time"])
    return points
