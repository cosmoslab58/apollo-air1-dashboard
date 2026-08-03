(function () {
  "use strict";

  // fmt / timeAgo / escapeHtml / bandVar / seriesFor / bandFromCo2 /
  // bandForVocIndex come from common.js; the SVG chart renderers
  // (measureWidth / renderChart / renderRowChart) from chart.js;
  // bandFromAqi / aqiFromConcentration / bandForConcentration from aqi.js.
  // Theme toggle, settings panel, and clock self-init in common.js.
  //
  // This page is the sensor's READINGS only -- live tiles + history charts.
  // Diagnostics and every device control (calibration, sleep, LED, factory
  // reset, home location) live on /device (device.js), reached from the
  // settings panel.

  /* ---------- temperature unit (F/C) ---------- */
  let currentUnit = localStorage.getItem("apollo-air1-unit") || "f";

  function tempUnitLabel() {
    return currentUnit === "f" ? "°F" : "°C";
  }
  // Absolute reading: F = C * 9/5 + 32.
  function displayTemp(celsius) {
    return typeof celsius === "number" ? (currentUnit === "f" ? celsius * 9 / 5 + 32 : celsius) : null;
  }

  function renderUnitToggle() {
    document.querySelectorAll(".unit-toggle").forEach((wrap) => {
      wrap.querySelectorAll("button").forEach((btn) => {
        btn.setAttribute("aria-pressed", String(btn.getAttribute("data-unit") === currentUnit));
      });
    });
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".unit-toggle button");
    if (!btn) return;
    currentUnit = btn.getAttribute("data-unit");
    localStorage.setItem("apollo-air1-unit", currentUnit);
    renderUnitToggle();
    loadLatest();
    loadHistory(currentRange);
  });

  // bandFromCo2 / bandForVocIndex / bandVar / seriesFor come from common.js;
  // measureWidth / renderChart / renderRowChart from chart.js (both loaded
  // first). Charts plot by real timestamp so sources sampled at different
  // rates overlay correctly.

  // Raw µg/m³ converted onto the shared 0-500 EPA AQI scale -- the same
  // non-technical default the dashboard and Technical page use.
  function toAqiSeries(points, parameter) {
    return points
      .map((p) => ({ t: p.t, v: aqiFromConcentration(parameter, p.v, "MICROGRAMS_PER_CUBIC_METER") }))
      .filter((p) => typeof p.v === "number");
  }

  // Rows colored by severity, not identity. Chart order mirrors the outdoor
  // (Technical) page and the readout tiles: AQI, then PM2.5/PM10, then the
  // non-standard PM1.0/PM4.0, then CO2, VOC/NOx, and finally the combined
  // weather chart -- matching the outdoor page's Temperature/Humidity/Pressure
  // grouping. PM2.5/PM10 read as AQI by default (Readout=Units switches them to
  // raw µg/m³); PM1.0/PM4.0 have no EPA-recognized health thresholds, so they
  // stay in µg/m³ and neutral-colored on their own chart.
  function renderInsideCharts(points, rangeLabel) {
    renderRowChart(document.getElementById("chart-aqi"), [
      { label: "AQI", unit: "", decimals: 0, bandFor: bandFromAqi, points: seriesFor(points, "aqi", null).points },
    ], { leftLabel: rangeLabel, label: "AQI history" });

    const units = readoutMode() === "units";
    document.getElementById("pm-chart-unit-label").textContent = units ? "µg/m³" : "AQI per pollutant";
    const pm25Points = seriesFor(points, "pm2_5_ugm3", null).points;
    const pm10Points = seriesFor(points, "pm10_0_ugm3", null).points;
    const pmRows = units ? [
      { label: "PM2.5", unit: " µg/m³", decimals: 1, bandFor: (v) => bandForConcentration("PM2.5", v, "MICROGRAMS_PER_CUBIC_METER"), points: pm25Points },
      { label: "PM10", unit: " µg/m³", decimals: 1, bandFor: (v) => bandForConcentration("PM10", v, "MICROGRAMS_PER_CUBIC_METER"), points: pm10Points },
    ] : [
      { label: "PM2.5 AQI", unit: "", decimals: 0, bandFor: bandFromAqi, points: toAqiSeries(pm25Points, "PM2.5") },
      { label: "PM10 AQI", unit: "", decimals: 0, bandFor: bandFromAqi, points: toAqiSeries(pm10Points, "PM10") },
    ];
    renderRowChart(document.getElementById("chart-pm"), pmRows, { leftLabel: rangeLabel, label: "PM2.5 / PM10 history" });

    renderRowChart(document.getElementById("chart-pm-fine"), [
      { label: "PM1.0", unit: " µg/m³", decimals: 1, bandFor: () => null, points: seriesFor(points, "pm1_0_ugm3", null).points },
      { label: "PM4.0", unit: " µg/m³", decimals: 1, bandFor: () => null, points: seriesFor(points, "pm4_0_ugm3", null).points },
    ], { leftLabel: rangeLabel, label: "PM1.0 / PM4.0 history" });

    renderRowChart(document.getElementById("chart-co2"), [
      { label: "CO2", unit: " ppm", decimals: 0, bandFor: bandFromCo2, points: seriesFor(points, "co2_ppm", null).points },
    ], { leftLabel: rangeLabel, label: "CO2 history" });

    renderRowChart(document.getElementById("chart-voc"), [
      { label: "VOC index", unit: "", decimals: 0, bandFor: bandForVocIndex, points: seriesFor(points, "voc_index", null).points },
      { label: "NOx index", unit: "", decimals: 0, bandFor: bandForNoxIndex, points: seriesFor(points, "nox_index", null).points },
    ], { leftLabel: rangeLabel, label: "VOC and NOx index history" });

    const tempPoints = seriesFor(points, "temperature_c", null).points.map(
      (p) => ({ t: p.t, v: currentUnit === "f" ? p.v * 9 / 5 + 32 : p.v }));
    renderRowChart(document.getElementById("chart-weather"), [
      { label: "Temperature", unit: ` ${tempUnitLabel()}`, decimals: 1, bandFor: () => null, points: tempPoints },
      { label: "Humidity", unit: " %", decimals: 1, bandFor: () => null, points: seriesFor(points, "humidity_pct", null).points },
      { label: "Pressure", unit: " hPa", decimals: 1, bandFor: () => null, points: seriesFor(points, "pressure_hpa", null).points },
    ], { leftLabel: rangeLabel, label: "Temperature, humidity, pressure history" });
  }

  /* ---------- live readout tiles ---------- */
  // The two PM tiles follow the app-wide Readout setting: AQI (the
  // non-technical default) or the sensor's raw µg/m³. Everything else has
  // no AQI equivalent (CO2/VOC/NOx are indoor-only scales; temp/humidity/
  // pressure aren't pollutants), so those tiles always show their own units.
  const isUnitsReadout = () => readoutMode() === "units";
  function pmTile(id, parameter, key) {
    return {
      id,
      label: () => isUnitsReadout() ? parameter : `${parameter} AQI`,
      unit: () => isUnitsReadout() ? "µg/m³" : "",
      key,
      decimals: () => isUnitsReadout() ? 1 : 0,
      band: (v) => bandForConcentration(parameter, v, "MICROGRAMS_PER_CUBIC_METER"),
      convert: (v) => isUnitsReadout() ? v : aqiFromConcentration(parameter, v, "MICROGRAMS_PER_CUBIC_METER"),
    };
  }
  // Tile order matches the chart order above (AQI-first, weather last).
  const READOUT_DEFS = [
    { id: "aqi", label: "AQI", unit: "", key: "aqi", decimals: 0, band: bandFromAqi },
    pmTile("pm25", "PM2.5", "pm2_5_ugm3"),
    pmTile("pm10", "PM10", "pm10_0_ugm3"),
    // bandFromCo2 (shared with Overview and this page's own CO2 chart, see
    // common.js) -- this tile used to have its own inline thresholds that
    // didn't match (e.g. never showed "good" green, only colored at
    // poor/bad), so CO2 looked color-coded on the dashboard but not here.
    { id: "co2", label: "CO2", unit: "ppm", key: "co2_ppm", decimals: 0, band: bandFromCo2 },
    { id: "voc", label: "VOC index", unit: "", key: "voc_index", decimals: 0, band: bandForVocIndex },
    // NOx is graded by the device (its own cuts, offset 1 rather than VOC's
    // 100) and drives the LED like any other channel, so it is coloured here
    // too. It used to be the one graded channel this page left grey.
    { id: "nox", label: "NOx index", unit: "", key: "nox_index", decimals: 0, band: bandForNoxIndex },
    { id: "temp", label: "Temperature", unit: () => tempUnitLabel(), key: "temperature_c", decimals: 1, band: () => null, convert: displayTemp },
    { id: "hum", label: "Humidity", unit: "%", key: "humidity_pct", decimals: 1, band: () => null },
    { id: "pressure", label: "Pressure", unit: "hPa", key: "pressure_hpa", decimals: 1, band: () => null },
  ];

  let previousLatest = null;

  function renderReadouts(latest) {
    const grid = document.getElementById("readout-grid");
    grid.innerHTML = READOUT_DEFS.map((r) => {
      const rawValue = latest ? latest[r.key] : null;
      const prevRawValue = previousLatest ? previousLatest[r.key] : null;
      let dir = "flat";
      if (typeof rawValue === "number" && typeof prevRawValue === "number" && rawValue !== prevRawValue) {
        dir = rawValue > prevRawValue ? "up" : "down";
      }
      // "–" for flat, not "→". A right-pointing arrow in a tile's top-right
      // corner is where every other interface in the world puts "open this",
      // and it read as one -- clicked expecting navigation, got nothing. The
      // up/down arrows were never ambiguous (nothing navigates upward), so
      // only the flat case changes, to the glyph that already means "no
      // change" beside a figure.
      const arrow = dir === "up" ? "↑" : dir === "down" ? "↓" : "–";
      // The glyph alone is decoration to a screen reader. Naming the direction
      // is also what makes it legible to anyone who reads "–" as a hyphen.
      const trendLabel = dir === "up" ? "trending up" : dir === "down" ? "trending down" : "no change";
      const bandKey = typeof rawValue === "number" ? r.band(rawValue) : null;
      const value = typeof r.convert === "function" ? r.convert(rawValue) : rawValue;
      const unit = typeof r.unit === "function" ? r.unit() : r.unit;
      const label = typeof r.label === "function" ? r.label() : r.label;
      const decimals = typeof r.decimals === "function" ? r.decimals() : r.decimals;
      return `<div class="readout" style="--edge-color: ${bandKey ? `var(--${bandKey})` : "var(--hairline)"}">
        <div class="r-label"><span>${label}</span><span class="trend" data-dir="${dir}" role="img" aria-label="${trendLabel}" title="${trendLabel} since the last reading">${arrow}</span></div>
        <div class="r-value">${fmt(value, decimals)}<span class="r-unit">${unit}</span></div>
      </div>`;
    }).join("");
  }

  /* ---------- indoor latest reading ---------- */
  async function loadLatest() {
    try {
      const res = await fetch("/api/latest");
      if (res.status === 404) {
        setIndoorUnavailable("Waiting for the AIR-1 to report in.");
        return;
      }
      if (!res.ok) throw new Error("request failed");
      const d = await res.json();

      renderReadouts(d);
      previousLatest = d;
      document.getElementById("since-reading").textContent = timeAgo(d.time);
    } catch (e) {
      setIndoorUnavailable("Couldn't reach the sensor feed.");
    }
  }

  // Matches Overview's rack empty-state (see dashboard.js's own
  // setIndoorUnavailable) -- the readout grid used to just go blank here,
  // leaving a tall empty gap between the "Live readout" head and History.
  function setIndoorUnavailable(msg) {
    document.getElementById("since-reading").textContent = "—";
    document.getElementById("readout-grid").innerHTML = `<div class="empty-state">${escapeHtml(msg)}</div>`;
    previousLatest = null;
  }

  /* ---------- history / charts ---------- */
  function rangeLabelFor(hours) {
    return { 6: "6h ago", 24: "24h ago", 72: "3d ago", 168: "7d ago" }[hours] || `${hours}h ago`;
  }

  // Charts measure their container's real width at render time (see
  // measureWidth), so a viewport change needs a re-render at the new width.
  // Caching the last-fetched points lets that happen instantly on resize
  // without a network round-trip.
  let lastInsidePoints = null, lastInsideRangeLabel = "";

  // Wrapped like every other loader here: this runs on a 60s poll, so a
  // phone that drops Wi-Fi would otherwise throw an unhandled rejection once
  // a minute. On failure the charts keep their last-rendered points rather
  // than being blanked -- stale data beats an empty page.
  async function loadHistory(hours) {
    try {
      const res = await fetch(`/api/history?hours=${hours}`);
      const points = res.ok ? await res.json() : [];
      lastInsidePoints = points;
      lastInsideRangeLabel = rangeLabelFor(hours);
      renderInsideCharts(points, lastInsideRangeLabel);
    } catch (e) {
      // Keep whatever is already on screen.
    }
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (lastInsidePoints) renderInsideCharts(lastInsidePoints, lastInsideRangeLabel);
    }, 200);
  });

  /* ---------- range toggle ---------- */
  let currentRange = 24;
  document.querySelectorAll("#range-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#range-toggle button").forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
      currentRange = Number(btn.getAttribute("data-range"));
      loadHistory(currentRange);
    });
  });

  // Settings panel's AQI/Units toggle (common.js) -- re-render the PM tiles
  // and PM chart at the new readout without refetching.
  document.addEventListener("readoutchange", () => {
    renderReadouts(previousLatest);
    if (lastInsidePoints) renderInsideCharts(lastInsidePoints, lastInsideRangeLabel);
  });

  /* ---------- init ---------- */
  renderUnitToggle();
  // Band table first -- see the same note in dashboard.js.
  bandTableReady.then(() => {
    loadLatest();
    loadHistory(currentRange);
  });
  // Readouts follow the device's own publish cadence -- see latestPollMs.
  // History stays on a flat 60s: it is a far heavier Flux query, and a chart
  // spanning hours gains much less from a 15s refresh than the current-value
  // tiles do.
  watchLatest(loadLatest, () => latestPollMs(previousLatest));
  pollInterval(() => { loadHistory(currentRange); }, 60000);
})();
