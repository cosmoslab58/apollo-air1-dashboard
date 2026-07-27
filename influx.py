import os

from influxdb_client import InfluxDBClient

MEASUREMENT = "air_quality"

FIELDS = [
    "co2_ppm", "pressure_hpa", "temperature_c", "humidity_pct",
    "pm1_0_ugm3", "pm2_5_ugm3", "pm4_0_ugm3", "pm10_0_ugm3",
    "voc_index", "nox_index", "aqi",
    # 0-5 worst-of severity band as the FIRMWARE graded it (-1 = nothing
    # readable). Deliberately not derived here or in the browser: the device
    # owns the thresholds that colour its own LED, and re-deriving them
    # elsewhere is how this page ends up disagreeing with the light on the
    # wall. The frontend also uses it to decide how fast to poll (see
    # latestPollMs in common.js), because a band above 0 is exactly what makes
    # the device publish every 15s instead of every 60s.
    #
    # Absent from any point written before the firmware that added it, so every
    # consumer has to tolerate None here, not just on a cold start.
    "air_band",
    "wifi_rssi_db", "esp_temperature_c", "uptime_s",
]
STRING_FIELDS = ["voc_quality", "firmware_version"]

_client = None


def get_client():
    global _client
    if _client is None:
        _client = InfluxDBClient(
            url=os.environ["INFLUX_URL"],
            token=os.environ["INFLUX_TOKEN"],
            org=os.environ["INFLUX_ORG"],
        )
    return _client


def _rows_from(tables):
    points = []
    for table in tables:
        for record in table.records:
            row = dict(record.values)
            row["time"] = record.get_time().isoformat()
            for key in ("result", "table", "_start", "_stop", "_measurement", "_time"):
                row.pop(key, None)
            points.append(row)
    return points


def _flux(fields, range_start, *, measurement, latest=False, sort=False):
    """Build the one Flux shape every query here uses: filter a measurement to
    a set of fields over a range, pivot fields into columns, and optionally
    take the latest point or sort by time. `latest` and `sort` are mutually
    exclusive in practice (latest collapses to one point)."""
    bucket = os.environ["INFLUX_BUCKET"]
    field_filter = " or ".join(f'r._field == "{f}"' for f in fields)
    pipeline = [
        f'from(bucket: "{bucket}")',
        f'|> range(start: {range_start})',
        f'|> filter(fn: (r) => r._measurement == "{measurement}")',
        f'|> filter(fn: (r) => {field_filter})',
    ]
    if latest:
        pipeline.append('|> last()')
    pipeline.append('|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")')
    if sort:
        pipeline.append('|> sort(columns: ["_time"])')
    return "\n  ".join(pipeline)


def _query(fields, range_start, *, measurement=None, latest=False, sort=False):
    measurement = measurement or OUTSIDE_MEASUREMENT
    flux = _flux(fields, range_start, measurement=measurement, latest=latest, sort=sort)
    return _rows_from(get_client().query_api().query(flux))


def query_latest():
    rows = _query(FIELDS + STRING_FIELDS, "-7d", measurement=MEASUREMENT, latest=True)
    return rows[-1] if rows else None


def query_history(hours):
    return _query(FIELDS + STRING_FIELDS, f"-{hours}h", measurement=MEASUREMENT, sort=True)


OUTSIDE_MEASUREMENT = "outside_air_quality"
OUTSIDE_FIELDS = ["aqi", "o3_aqi", "pm2_5_aqi", "pm10_aqi", "no2_aqi"]
OUTSIDE_STRING_FIELDS = ["category", "dominant_pollutant", "reporting_area"]


def query_outside_history(hours):
    return _query(OUTSIDE_FIELDS + OUTSIDE_STRING_FIELDS, f"-{hours}h", sort=True)


# Outside temperature/humidity/pressure -- not from AirNow or Google (neither
# gives weather), but from a separate feed written into this same
# measurement. Queried on its own, rather than folded into OUTSIDE_FIELDS,
# so pulling it in doesn't also drag in whichever AQI provider's pollutant
# fields happen to share the measurement.
OUTSIDE_WEATHER_FIELDS = ["temperature_c", "humidity_pct", "pressure_hpa"]

# OpenWeatherMap air_pollution components, written by Node-RED with an owm_
# prefix precisely so they stay separable from AirNow's fields in the same
# measurement (see the Apollo AIR-1 Node-RED tab). owm_aqi_epa/owm_category/
# owm_dominant_pollutant are the EPA AQI headline + labels Node-RED computes
# from the concentrations (the "Format OWM Pollution Fields & Tags" function)
# -- read straight back rather than recomputed here, same as the Google and
# PurpleAir feeds below.
OWM_FIELDS = [
    "owm_aqi_epa", "owm_aqi_index", "owm_pm2_5_ugm3", "owm_pm10_ugm3",
    "owm_o3_ugm3", "owm_no2_ugm3", "owm_so2_ugm3", "owm_co_ugm3", "owm_nh3_ugm3",
    # Per-pollutant AQI Node-RED derives from the concentrations above (NH3 has
    # no EPA breakpoint, so it stays concentration-only). See owm.py.
    "owm_pm2_5_aqi_epa", "owm_pm10_aqi_epa",
    "owm_o3_aqi_epa", "owm_no2_aqi_epa", "owm_so2_aqi_epa", "owm_co_aqi_epa",
]
OWM_STRING_FIELDS = ["owm_category", "owm_dominant_pollutant"]


def query_owm_latest():
    rows = _query(OWM_FIELDS + OWM_STRING_FIELDS, "-6h", latest=True)
    return rows[-1] if rows else None


def query_owm_history(hours):
    return _query(OWM_FIELDS + OWM_STRING_FIELDS, f"-{hours}h", sort=True)


def query_outside_weather(hours):
    return _query(OUTSIDE_WEATHER_FIELDS, f"-{hours}h", sort=True)


# Google and PurpleAir current-reading + history both come from here now --
# Node-RED polls both on a timer straight into Influx, the same pattern
# AirNow/OWM already use, instead of the dashboard calling either API
# itself. Prefixed field names follow the same collision-avoidance
# precedent as OWM_FIELDS above.
#
# The headline AQI, category, and dominant pollutant are read from the
# *_aqi_epa / *_category / *_dominant_pollutant fields Node-RED computes from
# the concentrations (its "Format ... Fields & Tags" functions) -- the app no
# longer recomputes them, so the number the dashboard shows, the number
# Grafana shows, and the number Node-RED derived are one and the same.
#
# Note the *_aqi_epa naming: an earlier google_aqi/purpleair_aqi field was
# written with inconsistent numeric types (int on some points, float on
# others), which makes InfluxDB v2's TSM engine panic ("interface conversion:
# tsm1.Value is tsm1.FloatValue, not tsm1.IntegerValue") the instant a query
# touches that series -- confirmed still fatal. Node-RED writes the AQI to the
# separate, always-float *_aqi_epa field precisely to sidestep that poisoned
# series; those legacy fields must never be queried.
GOOGLE_FIELDS = [
    "google_aqi_epa",
    "google_pm2_5_ugm3", "google_pm10_ugm3",
    "google_o3_ppb", "google_no2_ppb", "google_so2_ppb", "google_co_ppb",
    # Per-pollutant AQI Node-RED derives from the concentrations above, so the
    # dashboard shows every pollutant on the AQI scale (see google_aq.py).
    "google_pm2_5_aqi_epa", "google_pm10_aqi_epa",
    "google_o3_aqi_epa", "google_no2_aqi_epa", "google_so2_aqi_epa", "google_co_aqi_epa",
]
GOOGLE_STRING_FIELDS = [
    "google_category", "google_dominant_pollutant",
    "google_health_general", "google_health_children",
]

PURPLEAIR_FIELDS = [
    "purpleair_aqi_epa", "purpleair_pm2_5_ugm3", "purpleair_pm10_ugm3",
    # Per-pollutant AQI Node-RED derives from the corrected concentrations.
    "purpleair_pm2_5_aqi_epa", "purpleair_pm10_aqi_epa",
]
PURPLEAIR_STRING_FIELDS = ["purpleair_category", "purpleair_dominant_pollutant"]

# AirNow's own fields are the bare (unprefixed) ones in this measurement --
# Google/PurpleAir/OWM all carry a provider prefix precisely so AirNow's
# aqi/category/dominant_pollutant/reporting_area and its per-pollutant *_aqi
# numbers stay separable here. Node-RED already computes the AQI (AirNow's
# API hands it to us), so current conditions read straight from the DB like
# every other provider, rather than the app calling AirNow live.
AIRNOW_FIELDS = ["aqi", "pm2_5_aqi", "pm10_aqi", "o3_aqi", "no2_aqi", "co_aqi", "so2_aqi"]
AIRNOW_STRING_FIELDS = ["category", "dominant_pollutant", "reporting_area"]


def query_google_latest():
    return _latest(GOOGLE_FIELDS + GOOGLE_STRING_FIELDS)


def query_purpleair_latest():
    return _latest(PURPLEAIR_FIELDS + PURPLEAIR_STRING_FIELDS)


def query_airnow_latest():
    # AirNow updates hourly and can be sparse (its own reporting lags, and
    # not every hour publishes) -- a wider window than the other providers so
    # a gap doesn't blank the card when a recent-enough reading still exists.
    return _latest(AIRNOW_FIELDS + AIRNOW_STRING_FIELDS, range_start="-24h")


def _latest(fields, range_start="-6h"):
    rows = _query(fields, range_start, latest=True)
    return rows[-1] if rows else None


def query_google_history(hours):
    return _query(GOOGLE_FIELDS + GOOGLE_STRING_FIELDS, f"-{hours}h", sort=True)


def query_purpleair_history(hours):
    return _query(PURPLEAIR_FIELDS + PURPLEAIR_STRING_FIELDS, f"-{hours}h", sort=True)
