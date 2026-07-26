"""EPA AQI math shared by every provider: breakpoint tables, the
concentration -> AQI interpolation, category names, and the good/fair/poor/bad
severity bands. This is the single Python source of truth for AQI math; the
Node-RED formatters and static/aqi.js carry parallel copies that
tests/aqi_parity checks against these tables."""

# EPA's AQI breakpoint tables (current/2024 revision for PM2.5), each row
# [conc_lo, conc_hi, aqi_lo, aqi_hi]. Units are EPA's own: µg/m³ for
# particulates, ppb for O3/NO2/SO2, ppm for CO.
BREAKPOINTS = {
    "PM2.5": [(0.0, 9.0, 0, 50), (9.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
              (55.5, 125.4, 151, 200), (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 500)],
    "PM10": [(0, 54, 0, 50), (55, 154, 51, 100), (155, 254, 101, 150),
             (255, 354, 151, 200), (355, 424, 201, 300), (425, 604, 301, 500)],
    "O3": [(0, 54, 0, 50), (55, 70, 51, 100), (71, 85, 101, 150),
           (86, 105, 151, 200), (106, 200, 201, 300)],
    "NO2": [(0, 53, 0, 50), (54, 100, 51, 100), (101, 360, 101, 150),
            (361, 649, 151, 200), (650, 1249, 201, 300), (1250, 2049, 301, 500)],
    "SO2": [(0, 35, 0, 50), (36, 75, 51, 100), (76, 185, 101, 150), (186, 304, 151, 200)],
    "CO": [(0.0, 4.4, 0, 50), (4.5, 9.4, 51, 100), (9.5, 12.4, 101, 150), (12.5, 15.4, 151, 200)],
}

# Molecular weights for µg/m³ -> ppb conversion (at 25°C / 1 atm:
# ppb = µg/m³ × 24.45 / MW). OpenWeatherMap reports every gas in µg/m³;
# EPA's gas breakpoints are in ppb (ppm for CO).
MOLAR_MASS = {"O3": 48.00, "NO2": 46.01, "SO2": 64.07, "CO": 28.01, "NH3": 17.03}

CATEGORY_NAMES = [
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (500, "Hazardous"),
]


def ugm3_to_ppb(parameter, ugm3):
    mw = MOLAR_MASS.get(parameter)
    if mw is None or ugm3 is None:
        return None
    return ugm3 * 24.45 / mw


def epa_value(parameter, value, units=None):
    """Normalize a reported concentration into the unit that parameter's
    breakpoint table expects: µg/m³ for particulates, ppb for O3/NO2/SO2,
    ppm for CO. `units` is the provider's unit string ("PARTS_PER_BILLION" /
    "MICROGRAMS_PER_CUBIC_METER"); anything other than µg/m³ is taken as
    already-ppb, matching how the providers actually report.

    The Python twin of static/aqi.js's aqiEpaValue (tests/test_aqi_parity
    checks the two agree). Every caller of aqi_from_concentration must route
    through this -- CO especially, whose table is in ppm while every provider
    reports it in ppb or µg/m³: skipping the conversion inflates CO's
    sub-index ~1000x and makes it the dominant pollutant every time.
    """
    if value is None:
        return None
    if parameter in ("PM2.5", "PM10"):
        return value  # µg/m³ already, whatever the units string says
    ppb = ugm3_to_ppb(parameter, value) if units == "MICROGRAMS_PER_CUBIC_METER" else value
    if ppb is None:
        return None
    return ppb / 1000 if parameter == "CO" else ppb


def aqi_from_concentration(parameter, value):
    """value must already be in EPA's unit for the parameter (µg/m³ for
    PM2.5/PM10, ppb for O3/NO2/SO2, ppm for CO). Returns the EPA AQI number
    via EPA's piecewise-linear interpolation, or None."""
    table = BREAKPOINTS.get(parameter)
    if table is None or value is None:
        return None
    if value <= 0:
        return 0
    # EPA rounds concentrations before bucketing, leaving hairline gaps
    # between one bucket's high and the next's low that a raw unrounded
    # reading can land inside -- pick the last bucket whose low bound the
    # value has reached rather than requiring it to fall inside [lo, hi].
    row = table[0]
    for candidate in table:
        if value >= candidate[0]:
            row = candidate
        else:
            break
    conc_lo, conc_hi, aqi_lo, aqi_hi = row
    aqi = (aqi_hi - aqi_lo) / (conc_hi - conc_lo) * (value - conc_lo) + aqi_lo
    return round(max(0, min(500, aqi)))


def category_name(aqi):
    if aqi is None:
        return None
    for ceiling, name in CATEGORY_NAMES:
        if aqi <= ceiling:
            return name
    return "Hazardous"


def band_for_aqi(aqi):
    """The good/fair/poor/bad split used for both indoor and outdoor AQI, so
    every card is directly comparable at a glance. Provider-agnostic -- lives
    here (not in airnow.py) since all providers band the same way."""
    if aqi is None:
        return None
    if aqi > 150:
        return "bad"
    if aqi > 100:
        return "poor"
    if aqi > 50:
        return "fair"
    return "good"


def band_for_category(number):
    """Fallback for forecast rows where AirNow reports AQI as -1 (not
    computed, e.g. during an active smoke/alert day) but still gives a
    Category.Number on its own 1-6 scale. Thresholds mirror band_for_aqi."""
    if number is None:
        return None
    if number >= 4:
        return "bad"
    if number == 3:
        return "poor"
    if number == 2:
        return "fair"
    return "good"
