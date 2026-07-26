(function () {
  "use strict";

  // fmt / timeAgo / escapeHtml / bandVar / seriesFor / bandFromCo2 /
  // bandForVocIndex come from common.js; the SVG chart renderers
  // (measureWidth / renderChart / renderRowChart) from chart.js;
  // bandFromAqi / aqiFromConcentration / bandForConcentration from aqi.js.
  // Theme toggle, settings panel, and clock self-init in common.js.

  /* ---------- temperature unit (F/C) ---------- */
  let currentUnit = localStorage.getItem("apollo-air1-unit") || "f";

  function tempUnitLabel() {
    return currentUnit === "f" ? "°F" : "°C";
  }
  // Absolute reading: F = C * 9/5 + 32.
  function displayTemp(celsius) {
    return typeof celsius === "number" ? (currentUnit === "f" ? celsius * 9 / 5 + 32 : celsius) : null;
  }
  // A *difference* between two temperatures (e.g. a calibration offset)
  // converts without the +32 -- that's only for absolute readings.
  function displayTempDelta(deltaCelsius) {
    return typeof deltaCelsius === "number" ? (currentUnit === "f" ? deltaCelsius * 9 / 5 : deltaCelsius) : null;
  }

  function renderUnitToggle() {
    document.querySelectorAll(".unit-toggle").forEach((wrap) => {
      wrap.querySelectorAll("button").forEach((btn) => {
        btn.setAttribute("aria-pressed", String(btn.getAttribute("data-unit") === currentUnit));
      });
    });
    document.querySelectorAll("#unit-toffset").forEach((el) => {
      el.textContent = tempUnitLabel();
    });
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".unit-toggle button");
    if (!btn) return;
    currentUnit = btn.getAttribute("data-unit");
    localStorage.setItem("apollo-air1-unit", currentUnit);
    renderUnitToggle();
    loadLatest();
    loadControls();
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
      { label: "NOx index", unit: "", decimals: 0, bandFor: () => null, points: seriesFor(points, "nox_index", null).points },
    ], { leftLabel: rangeLabel, label: "VOC and NOx index history" });

    const tempPoints = seriesFor(points, "temperature_c", null).points.map(
      (p) => ({ t: p.t, v: currentUnit === "f" ? p.v * 9 / 5 + 32 : p.v }));
    renderRowChart(document.getElementById("chart-weather"), [
      { label: "Temperature", unit: ` ${tempUnitLabel()}`, decimals: 1, bandFor: () => null, points: tempPoints },
      { label: "Humidity", unit: " %", decimals: 1, bandFor: () => null, points: seriesFor(points, "humidity_pct", null).points },
      { label: "Pressure", unit: " hPa", decimals: 1, bandFor: () => null, points: seriesFor(points, "pressure_hpa", null).points },
    ], { leftLabel: rangeLabel, label: "Temperature, humidity, pressure history" });
  }

  /* ---------- toast ---------- */
  function toast(msg) {
    const stack = document.getElementById("toast-stack");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 3200);
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
    { id: "nox", label: "NOx index", unit: "", key: "nox_index", decimals: 0, band: () => null },
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
      const arrow = dir === "up" ? "↑" : dir === "down" ? "↓" : "→";
      const bandKey = typeof rawValue === "number" ? r.band(rawValue) : null;
      const value = typeof r.convert === "function" ? r.convert(rawValue) : rawValue;
      const unit = typeof r.unit === "function" ? r.unit() : r.unit;
      const label = typeof r.label === "function" ? r.label() : r.label;
      const decimals = typeof r.decimals === "function" ? r.decimals() : r.decimals;
      return `<div class="readout" style="--edge-color: ${bandKey ? `var(--${bandKey})` : "var(--hairline)"}">
        <div class="r-label"><span>${label}</span><span class="trend" data-dir="${dir}">${arrow}</span></div>
        <div class="r-value">${fmt(value, decimals)}<span class="r-unit">${unit}</span></div>
      </div>`;
    }).join("");
  }

  /* ---------- indoor latest reading ---------- */
  const INDOOR_FIELD_IDS = ["d-rssi", "d-esptemp", "d-uptime", "d-firmware"];

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

      document.getElementById("d-rssi").textContent = fmt(d.wifi_rssi_db, 0) + " dB";
      document.getElementById("d-esptemp").textContent = fmt(displayTemp(d.esp_temperature_c), 1) + " " + tempUnitLabel();
      const uptimeMin = typeof d.uptime_s === "number" ? d.uptime_s / 60 : null;
      document.getElementById("d-uptime").textContent = fmt(uptimeMin, 1) + " min";
      document.getElementById("d-firmware").textContent = d.firmware_version || "—";
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
    INDOOR_FIELD_IDS.forEach((id) => { document.getElementById(id).textContent = "—"; });
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

  /* ---------- controls (real MQTT bridge) -- device setup ---------- */
  const stepperConf = {
    sleep: { object_id: "sleep_duration", step: 1, min: 0, max: 800, digits: 0, stateKey: "sleep_duration_min" },
    toffset: { object_id: "sen55_temperature_offset", step: 0.5, min: -70, max: 70, digits: 1, stateKey: "sen55_temperature_offset", isTempDelta: true },
    hoffset: { object_id: "sen55_humidity_offset", step: 0.5, min: -70, max: 70, digits: 1, stateKey: "sen55_humidity_offset" },
    poffset: { object_id: "dps310_pressure_offset", step: 1, min: -100, max: 100, digits: 1, stateKey: "dps310_pressure_offset" },
  };
  const stepperState = { sleep: null, toffset: null, hoffset: null, poffset: null };

  // The stored/sent value always stays in the device's native °C -- only
  // the displayed text converts, so the +/- step size and what's posted to
  // the backend never change with the unit toggle.
  function displayStepperValue(conf, rawValue) {
    const value = conf.isTempDelta ? displayTempDelta(rawValue) : rawValue;
    return value.toFixed(conf.digits);
  }

  async function postControl(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "request failed");
    }
  }

  async function loadControls() {
    try {
      const res = await fetch("/api/controls");
      if (!res.ok) throw new Error("request failed");
      const s = await res.json();

      const rocker = document.getElementById("rocker-sleep");
      rocker.setAttribute("aria-pressed", String(!!s.prevent_sleep));

      // Don't fight the user mid-drag: only adopt the device's value when the
      // slider isn't focused, or a poll landing between drag and publish would
      // snap the handle back.
      const bright = document.getElementById("led-brightness");
      if (typeof s.led_brightness === "number" && document.activeElement !== bright) {
        bright.value = String(s.led_brightness);
        document.getElementById("val-led-brightness").textContent = String(Math.round(s.led_brightness));
      }
      document.getElementById("rocker-led-mode")
        .setAttribute("aria-pressed", String(!!s.led_alarm_mode));
      syncLedBrightnessHelp(!!s.led_alarm_mode);

      Object.entries(stepperConf).forEach(([key, conf]) => {
        const v = s[conf.stateKey];
        if (typeof v === "number") {
          stepperState[key] = v;
          document.getElementById("val-" + key).textContent = displayStepperValue(conf, v);
        }
      });
    } catch (e) {
      // Controls staying at their last-known display is preferable to blanking them out.
    }
  }

  document.querySelectorAll("[data-step]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const key = btn.getAttribute("data-step");
      const dir = Number(btn.getAttribute("data-dir"));
      const conf = stepperConf[key];
      const base = stepperState[key] ?? 0;
      let v = base + dir * conf.step;
      v = Math.max(conf.min, Math.min(conf.max, v));
      v = Math.round(v * 10) / 10;
      stepperState[key] = v;
      document.getElementById("val-" + key).textContent = displayStepperValue(conf, v);
      try {
        await postControl(`/api/control/number/${conf.object_id}`, { value: v });
        toast("Sent — applies next time the device wakes");
      } catch (e) {
        toast("Couldn't send that — " + e.message);
      }
    });
  });

  const rockerSleep = document.getElementById("rocker-sleep");
  rockerSleep.addEventListener("click", async () => {
    const on = rockerSleep.getAttribute("aria-pressed") !== "true";
    rockerSleep.setAttribute("aria-pressed", String(on));
    try {
      await postControl("/api/control/switch/prevent_sleep", { state: on });
      toast(on
        ? "Sent — stays awake once it's next connected"
        : "Sent — resumes its normal sleep cycle next wake");
    } catch (e) {
      rockerSleep.setAttribute("aria-pressed", String(!on));
      toast("Couldn't send that — " + e.message);
    }
  });

  // Both LED toggles follow the prevent_sleep pattern: flip optimistically so
  // the control feels immediate, revert on failure. The device applies these
  // straight away rather than at next wake, since it is mains-powered.
  function wireLedRocker(elementId, objectId, onMessage, offMessage, onToggle) {
    const el = document.getElementById(elementId);
    el.addEventListener("click", async () => {
      const on = el.getAttribute("aria-pressed") !== "true";
      el.setAttribute("aria-pressed", String(on));
      if (onToggle) onToggle(on);
      try {
        await postControl(`/api/control/switch/${objectId}`, { state: on });
        toast(on ? onMessage : offMessage);
      } catch (e) {
        el.setAttribute("aria-pressed", String(!on));
        if (onToggle) onToggle(!on);
        toast("Couldn't send that — " + e.message);
      }
    });
  }

  // Publish on release rather than on every input event -- dragging fires
  // input continuously and each one is an MQTT publish. The label still tracks
  // the handle live so the control feels responsive.
  const ledBrightness = document.getElementById("led-brightness");
  const ledBrightnessLabel = document.getElementById("val-led-brightness");
  const ledBrightnessHelp = document.getElementById("help-led-brightness");

  // The two controls are independent in the firmware, so the help text is the
  // only thing that moves: the slider governs the steady color and nothing
  // else, which makes 0 mean "dark between alarms" rather than "LED off"
  // whenever the strobe is armed. Neither control is ever disabled -- both
  // always do something.
  function syncLedBrightnessHelp(alarmOn) {
    ledBrightnessHelp.textContent = alarmOn
      ? "0 leaves the LED dark until the strobe fires — the strobe ignores this"
      : "0 turns the LED off entirely";
  }

  ledBrightness.addEventListener("input", () => {
    ledBrightnessLabel.textContent = ledBrightness.value;
  });
  ledBrightness.addEventListener("change", async () => {
    const v = Number(ledBrightness.value);
    // 0 means different things either side of the alarm switch, and calling it
    // "LED off" while the strobe is still armed would be a lie.
    const alarmOn = document.getElementById("rocker-led-mode")
      .getAttribute("aria-pressed") === "true";
    try {
      await postControl("/api/control/number/led_brightness", { value: v });
      toast(v > 0 ? `LED brightness ${v}%`
        : alarmOn ? "Dark between alarms — the strobe still fires" : "LED off");
    } catch (e) {
      toast("Couldn't send that — " + e.message);
    }
  });

  wireLedRocker("rocker-led-mode", "led_alarm_mode",
    "Alarm mode — the strobe is armed",
    "Alarm off — no strobe, whatever the readings",
    syncLedBrightnessHelp);

  async function pressButton(objectId, sentMessage) {
    try {
      await postControl(`/api/control/button/${objectId}`);
      toast(sentMessage);
    } catch (e) {
      toast("Couldn't send that — " + e.message);
    }
  }
  document.getElementById("btn-calibrate").addEventListener("click", () => {
    pressButton("calibrate_scd40_to_420ppm", "Sent — calibrates next time the device wakes");
  });
  document.getElementById("btn-clean").addEventListener("click", () => {
    pressButton("clean_sen55", "Sent — cleans next time the device wakes");
  });
  document.getElementById("btn-reboot").addEventListener("click", () => {
    pressButton("esp_reboot", "Sent — restarts if the device is currently awake");
  });

  const holdBtn = document.getElementById("btn-factory-reset");
  const holdFill = document.getElementById("hold-fill");
  let holdTimer = null, holdStart = 0;
  const HOLD_MS = 3000;
  function holdStep() {
    const pct = Math.min(100, ((Date.now() - holdStart) / HOLD_MS) * 100);
    holdFill.style.width = pct + "%";
    if (pct >= 100) {
      cancelHold();
      pressButton("factory_reset_esp", "Sent — factory reset applies if the device is currently awake");
      return;
    }
    holdTimer = requestAnimationFrame(holdStep);
  }
  function startHold() { holdStart = Date.now(); holdTimer = requestAnimationFrame(holdStep); }
  function cancelHold() { if (holdTimer) cancelAnimationFrame(holdTimer); holdTimer = null; holdFill.style.width = "0%"; }
  holdBtn.addEventListener("mousedown", startHold);
  holdBtn.addEventListener("touchstart", startHold, { passive: true });
  ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach((ev) => holdBtn.addEventListener(ev, cancelHold));

  /* ---------- home location editor ----------
   * Deliberately rare to touch: it repoints what Node-RED polls and logs to
   * InfluxDB (see home_config.py), unlike Away (edited from the header's
   * settings panel on every page). Living here rather than in that popover
   * is the friction -- same "Setup" section as the sleep/calibration/
   * factory-reset controls above, which are also rarely-touched device
   * config. This is the same form the old /away page used to host. */

  // PurpleAir sensor status isn't repeated here -- it's already reflected on
  // the dashboard itself (the provider chip goes unavailable, with the real
  // reason as its hover title, when there's no healthy sensor nearby).
  function renderHome(home) {
    const el = document.getElementById("home-current");
    if (!home || !home.zip) { el.textContent = "No home set"; return; }
    const where = home.reporting_area || home.location_slug || home.zip;
    el.innerHTML = `${escapeHtml(where)} <span class="eyebrow">(ZIP ${escapeHtml(home.zip)})</span>`;
  }

  async function loadHome() {
    try {
      const res = await fetch("/api/home");
      renderHome(res.ok ? await res.json() : null);
    } catch (e) {
      renderHome(null);
    }
  }

  // The save resolves the nearest PurpleAir sensor server-side, same as
  // Away's own zip-entry flow -- no separate preview step needed, the toast
  // just reports what got picked after the fact.
  document.getElementById("home-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const zip = document.getElementById("home-zip").value;
    const label = document.getElementById("home-label").value;
    const coordsRaw = document.getElementById("home-coords").value.trim();

    // Optional -- pins the PurpleAir search + Google/OWM forecast to an
    // exact point instead of wherever AirNow resolves the zip to (see the
    // away-hint text). Blank is fine (falls back to that zip resolution);
    // anything entered has to actually parse as "lat, lon", though.
    let lat = null, lon = null;
    if (coordsRaw) {
      const parts = coordsRaw.split(",").map((p) => Number(p.trim()));
      if (parts.length !== 2 || parts.some(Number.isNaN)) {
        toast("Coordinates should look like: 42.5988, -83.3577");
        return;
      }
      [lat, lon] = parts;
    }

    try {
      const res = await fetch("/api/home", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ zip, label, lat, lon }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.error || "request failed");
      renderHome(d.home);
      document.getElementById("home-zip").value = "";
      document.getElementById("home-label").value = "";
      document.getElementById("home-coords").value = "";
      const s = d.purpleair;
      const sensorMsg = s ? ` — using PurpleAir ${s.name || "#" + s.index} (${s.distance_km} km)` : " — no PurpleAir sensor nearby";
      toast((d.published ? "Home updated" : "Home saved (Node-RED will pick it up when the broker reconnects)") + sensorMsg);
    } catch (err) {
      toast("Couldn't save — " + err.message);
    }
  });

  loadHome();

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
  loadLatest();
  loadHistory(currentRange);
  loadControls();
  pollInterval(loadLatest, 60000);
  pollInterval(loadControls, 30000);
  pollInterval(() => { loadHistory(currentRange); }, 60000);
})();
