// Shared EPA AQI math for the browser -- the single frontend copy of the
// breakpoint tables + concentration->AQI interpolation + severity bands.
// Loaded before each page's own script (index/forecast/technical/indoor), so
// its top-level functions are visible inside those scripts' IIFEs. Kept in
// lockstep with epa_aqi.py; tests/test_aqi_parity.py asserts the table below
// matches epa_aqi.BREAKPOINTS so the two languages can't silently drift.

// Each row is [concLo, concHi, aqiLo, aqiHi] in EPA's own units: µg/m³ for
// particulates, ppb for O3/NO2/SO2, ppm for CO.
const AQI_BREAKPOINTS = {
  "PM2.5": [[0.0, 9.0, 0, 50], [9.1, 35.4, 51, 100], [35.5, 55.4, 101, 150], [55.5, 125.4, 151, 200], [125.5, 225.4, 201, 300], [225.5, 325.4, 301, 500]],
  "PM10": [[0, 54, 0, 50], [55, 154, 51, 100], [155, 254, 101, 150], [255, 354, 151, 200], [355, 424, 201, 300], [425, 604, 301, 500]],
  "O3": [[0, 54, 0, 50], [55, 70, 51, 100], [71, 85, 101, 150], [86, 105, 151, 200], [106, 200, 201, 300]],
  "NO2": [[0, 53, 0, 50], [54, 100, 51, 100], [101, 360, 101, 150], [361, 649, 151, 200], [650, 1249, 201, 300], [1250, 2049, 301, 500]],
  "SO2": [[0, 35, 0, 50], [36, 75, 51, 100], [76, 185, 101, 150], [186, 304, 151, 200]],
  "CO": [[0.0, 4.4, 0, 50], [4.5, 9.4, 51, 100], [9.5, 12.4, 101, 150], [12.5, 15.4, 151, 200]]
};

// Molar masses for µg/m³ -> ppb (ppb = µg/m³ × 24.45 / MW), matching
// epa_aqi.py. OpenWeatherMap reports gases in µg/m³, so they must be converted
// before the ppb/ppm breakpoints apply; Google already reports ppb.
const AQI_MOLAR_MASS = { O3: 48.0, NO2: 46.01, SO2: 64.07, CO: 28.01 };

// Convert a reported concentration into the unit the EPA breakpoint table for
// that parameter expects. Handles both provider unit conventions so a µg/m³
// gas reading isn't miscompared as if it were already ppb.
function aqiEpaValue(parameter, value, units) {
  if (parameter === "PM2.5" || parameter === "PM10") return value; // µg/m³ already
  let ppb = value;
  if (units === "MICROGRAMS_PER_CUBIC_METER") {
    const mw = AQI_MOLAR_MASS[parameter];
    if (!mw) return null;
    ppb = (value * 24.45) / mw;
  }
  return parameter === "CO" ? ppb / 1000 : ppb; // EPA's CO table is ppm
}

function aqiFromConcentration(parameter, value, units) {
  const table = AQI_BREAKPOINTS[parameter];
  if (typeof value !== "number" || !table) return null;
  const v = aqiEpaValue(parameter, value, units);
  if (v === null) return null;
  if (v <= 0) return 0;
  let row = table[0];
  for (const candidate of table) {
    if (v >= candidate[0]) row = candidate;
    else break;
  }
  const [concLo, concHi, aqiLo, aqiHi] = row;
  const aqi = ((aqiHi - aqiLo) / (concHi - concLo)) * (v - concLo) + aqiLo;
  return Math.round(Math.max(0, Math.min(500, aqi)));
}

// Bands come from the device's published table via bandForChannel (common.js),
// not from cutoffs written here -- see the header comment on that section. This
// used to hardcode 50/100/150, which agreed with the firmware's first three
// boundaries by luck and then stopped at "bad" where the firmware has two more
// bands above it.
//
// Note the split of responsibility: the breakpoint table above turns a
// CONCENTRATION into an AQI number, and that is EPA's published standard, safe
// to implement in both places. What counts as "poor" *for a given AQI number*
// is a severity judgement, and that comes from the device.
//
// Load order matters: aqi.js is included before common.js on every page, so
// bandForChannel is not defined while this file is being parsed -- only when
// these functions are eventually called. That is fine, and the guard keeps a
// stray early call from throwing.
function bandFromAqi(aqi) {
  if (typeof bandForChannel !== "function") return null;
  return bandForChannel("aqi", aqi);
}

function bandForConcentration(parameter, value, units) {
  return bandFromAqi(aqiFromConcentration(parameter, value, units));
}
