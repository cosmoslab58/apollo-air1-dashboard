(function () {
  "use strict";

  // fmt / timeAgo / bandVar / escapeHtml / formatConcentrationUnits /
  // seriesFor / readoutMode and the provider constants come from common.js;
  // the SVG chart renderers (measureWidth / renderRowChart /
  // renderOverlayRowChart) from chart.js; bandFromAqi / aqiFromConcentration /
  // bandForConcentration from aqi.js. Theme toggle, settings panel, and clock
  // self-init in common.js.

  function formatObservedHour(hour) {
    if (typeof hour !== "number") return "—";
    const d = new Date();
    d.setHours(hour, 0, 0, 0);
    return d.toLocaleTimeString([], { hour: "numeric" });
  }

  /* ---------- temperature unit (F/C) -- the Inside vs Outside weather
     overlay chart needs this even though this page has no indoor readouts
     of its own. ---------- */
  let currentUnit = localStorage.getItem("apollo-air1-unit") || "f";

  function tempUnitLabel() {
    return currentUnit === "f" ? "°F" : "°C";
  }
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
    loadOutsideHistorySection(currentRangeOutside);
  });

  // bandVar / measureWidth / renderRowChart / renderOverlayRowChart come from
  // common.js + chart.js (loaded first). Charts plot by real timestamp so
  // sources sampled at different rates (indoor ~5-10min vs AirNow hourly)
  // overlay correctly.

  // defs: [key, label, unit, decimals, bandFor(v)][]
  function renderPollutantRows(chartElId, points, defs, rangeLabel, chartLabel) {
    const rows = defs.map(([key, label, unit, decimals, bandFor]) => ({
      label, unit, decimals, bandFor,
      points: seriesFor(points, key, null).points,
    }));
    renderRowChart(document.getElementById(chartElId), rows, { leftLabel: rangeLabel, label: chartLabel });
  }

  function toAqiSeries(points, parameter, units) {
    return points
      .map((p) => ({ t: p.t, v: aqiFromConcentration(parameter, p.v, units) }))
      .filter((p) => typeof p.v === "number");
  }

  // Same shape as renderPollutantRows, but each raw concentration series is
  // converted onto the 0-500 AQI scale first -- the Readout=AQI counterpart
  // used for the non-technical default. defs: [key, label, parameter, units][]
  function renderPollutantAqiRows(chartElId, points, defs, rangeLabel, chartLabel) {
    const rows = defs.map(([key, label, parameter, units]) => ({
      label, unit: "", decimals: 0, bandFor: bandFromAqi,
      points: toAqiSeries(seriesFor(points, key, null).points, parameter, units),
    }));
    renderRowChart(document.getElementById(chartElId), rows, { leftLabel: rangeLabel, label: chartLabel });
  }

  // AirNow is the only provider that gives *only* a computed AQI, never a
  // raw concentration -- Google, PurpleAir, and OpenWeatherMap all report
  // real µg/m³/ppb numbers (owm.py and purpleair.py deliberately reuse
  // google_aq.py's own field names -- pm2_5_ugm3, o3_ppb, etc. -- for
  // exactly this reason), so they share the same "raw concentration"
  // branch as Google rather than each needing their own case.
  function providerGivesConcentrations() {
    return currentProvider() !== "airnow";
  }

  function providerLabel() {
    return PROVIDER_NAMES[currentProvider()] || "AirNow";
  }

  // insidePoints/outsidePoints are both needed on the same time axis --
  // called from the same place that already fetches both (see
  // loadOutsideHistorySection) rather than each chart fetching its own copy.
  //
  // These three cards render in the same order for every mode/provider
  // combination (see the fixed markup in technical.html) -- only their
  // content degrades gracefully (a missing row, or an empty state) when the
  // current combination has nothing for it, rather than the card itself
  // disappearing and reflowing whatever comes after it.
  //
  // In Home mode the AQI and PM cards overlay the indoor series (faded)
  // right on the same chart instead of a separate Inside-vs-Outside section
  // repeating the outside lines. AQI is the one number every provider gives
  // on a shared 0-500 scale, so it always overlays. PM only overlays where
  // AIR-1 and the selected provider measure the *same* thing: providers that
  // give raw concentrations overlay PM2.5/PM10 (already µg/m³ on both sides)
  // directly. AirNow never gives a raw concentration for anything -- only
  // computed AQI numbers -- so AIR-1's own raw PM2.5/PM10 convert to an
  // actual EPA AQI number (toAqiSeries/aqiFromConcentration) and overlay
  // against AirNow's pm2_5_aqi/pm10_aqi on that shared scale instead. CO/NO2
  // needed the optional MICS-4514, which this unit doesn't have, so those
  // never overlay. O3 and SO2 have no indoor equivalent either (no ozone or
  // SO2 sensor at all), so the gas card stays outside-only. In Away mode
  // there's no indoor series to compare against, so every card is
  // outside-only.
  function renderOutsideCharts(insidePoints, outsidePoints, rangeLabel) {
    const home = currentMode() !== "away";

    // The one field every provider's history carries in every mode,
    // including AirNow's coarse Away series -- always safe to plot.
    if (home) {
      renderOverlayRowChart(document.getElementById("chart-outside-aqi-history"), [
        {
          label: "AQI", unit: "", decimals: 0,
          inside: { points: seriesFor(insidePoints, "aqi", null).points, bandFor: bandFromAqi },
          outside: { points: seriesFor(outsidePoints, "aqi", null).points, bandFor: bandFromAqi },
        },
      ], { leftLabel: rangeLabel, label: "Inside vs outside AQI history" });
    } else {
      renderPollutantRows("chart-outside-aqi-history", outsidePoints, [
        ["aqi", "AQI", "", 0, bandFromAqi],
      ], rangeLabel, "Outside AQI history");
    }

    const showUnits = readoutMode() === "units";
    const pmEl = document.getElementById("chart-outside-pm-history");
    const pmBand25 = (v) => bandForConcentration("PM2.5", v, "MICROGRAMS_PER_CUBIC_METER");
    const pmBand10 = (v) => bandForConcentration("PM10", v, "MICROGRAMS_PER_CUBIC_METER");
    if (showUnits && providerGivesConcentrations()) {
      // Readout=Units reads the provider's real µg/m³ numbers directly --
      // only available for providers that report them.
      document.getElementById("pm-history-unit-label").textContent = "µg/m³";
      if (home) {
        renderOverlayRowChart(pmEl, [
          {
            label: "PM2.5", unit: " µg/m³", decimals: 1,
            inside: { points: seriesFor(insidePoints, "pm2_5_ugm3", null).points, bandFor: pmBand25 },
            outside: { points: seriesFor(outsidePoints, "pm2_5_ugm3", null).points, bandFor: pmBand25 },
          },
          {
            label: "PM10", unit: " µg/m³", decimals: 1,
            inside: { points: seriesFor(insidePoints, "pm10_0_ugm3", null).points, bandFor: pmBand10 },
            outside: { points: seriesFor(outsidePoints, "pm10_ugm3", null).points, bandFor: pmBand10 },
          },
        ], { leftLabel: rangeLabel, label: "Inside vs outside pollutant concentration history" });
      } else {
        renderPollutantRows("chart-outside-pm-history", outsidePoints, [
          ["pm2_5_ugm3", "PM2.5", " µg/m³", 1, pmBand25],
          ["pm10_ugm3", "PM10", " µg/m³", 1, pmBand10],
        ], rangeLabel, "Outside PM2.5/PM10 history");
      }
    } else {
      // AQI is the default (non-technical) read regardless of provider.
      // Outside comes straight from AirNow's own pm2_5_aqi/pm10_aqi when it
      // has no raw concentration to convert (those are only ever populated
      // in Home -- its Away history carries no per-pollutant breakdown at
      // all, see away.py, so the chart falls back to its "no data" state
      // there); every other provider's history only ever carries the
      // concentration, so that's converted here.
      document.getElementById("pm-history-unit-label").textContent = "AQI per pollutant";
      const outsidePm25 = providerGivesConcentrations()
        ? toAqiSeries(seriesFor(outsidePoints, "pm2_5_ugm3", null).points, "PM2.5", "MICROGRAMS_PER_CUBIC_METER")
        : seriesFor(outsidePoints, "pm2_5_aqi", null).points;
      const outsidePm10 = providerGivesConcentrations()
        ? toAqiSeries(seriesFor(outsidePoints, "pm10_ugm3", null).points, "PM10", "MICROGRAMS_PER_CUBIC_METER")
        : seriesFor(outsidePoints, "pm10_aqi", null).points;
      if (home) {
        renderOverlayRowChart(pmEl, [
          {
            label: "PM2.5 AQI", unit: "", decimals: 0,
            inside: { points: toAqiSeries(seriesFor(insidePoints, "pm2_5_ugm3", null).points, "PM2.5", "MICROGRAMS_PER_CUBIC_METER"), bandFor: bandFromAqi },
            outside: { points: outsidePm25, bandFor: bandFromAqi },
          },
          {
            label: "PM10 AQI", unit: "", decimals: 0,
            inside: { points: toAqiSeries(seriesFor(insidePoints, "pm10_0_ugm3", null).points, "PM10", "MICROGRAMS_PER_CUBIC_METER"), bandFor: bandFromAqi },
            outside: { points: outsidePm10, bandFor: bandFromAqi },
          },
        ], { leftLabel: rangeLabel, label: "Inside vs outside pollutant AQI history" });
      } else {
        renderRowChart(pmEl, [
          { label: "PM2.5", unit: "", decimals: 0, bandFor: bandFromAqi, points: outsidePm25 },
          { label: "PM10", unit: "", decimals: 0, bandFor: bandFromAqi, points: outsidePm10 },
        ], { leftLabel: rangeLabel, label: "Outside PM2.5/PM10 AQI history" });
      }
    }

    // Everything beyond PM2.5/PM10: AirNow gives per-pollutant AQI (O3/NO2
    // only -- its history carries no CO/SO2), Google/OpenWeatherMap give raw
    // gas concentrations (same field names, see providerGivesConcentrations),
    // and PurpleAir is PM-only -- a single physical sensor with no gas
    // channel at all -- so it gets an explicit empty state rather than a
    // chart that looks like it's just waiting on data.
    const pollutantsLabel = document.getElementById("pollutants-unit-label");
    if (currentProvider() === "airnow") {
      pollutantsLabel.textContent = "AQI per pollutant";
      renderPollutantRows("chart-outside-pollutants", outsidePoints, [
        ["o3_aqi", "O3", "", 0, bandFromAqi],
        ["no2_aqi", "NO2", "", 0, bandFromAqi],
      ], rangeLabel, "Outside pollutant AQI history");
    } else if (currentProvider() === "purpleair") {
      pollutantsLabel.textContent = "—";
      document.getElementById("chart-outside-pollutants").innerHTML =
        '<div class="empty-state">PurpleAir reports particulates only -- no gas pollutants.</div>';
    } else if (showUnits) {
      pollutantsLabel.textContent = "ppb";
      renderPollutantRows("chart-outside-pollutants", outsidePoints, [
        ["o3_ppb", "O3", " ppb", 1, (v) => bandForConcentration("O3", v, "PARTS_PER_BILLION")],
        ["no2_ppb", "NO2", " ppb", 2, (v) => bandForConcentration("NO2", v, "PARTS_PER_BILLION")],
        ["so2_ppb", "SO2", " ppb", 2, (v) => bandForConcentration("SO2", v, "PARTS_PER_BILLION")],
        ["co_ppb", "CO", " ppb", 2, (v) => bandForConcentration("CO", v, "PARTS_PER_BILLION")],
      ], rangeLabel, "Outside gas concentration history");
    } else {
      pollutantsLabel.textContent = "AQI per pollutant";
      renderPollutantAqiRows("chart-outside-pollutants", outsidePoints, [
        ["o3_ppb", "O3", "O3", "PARTS_PER_BILLION"],
        ["no2_ppb", "NO2", "NO2", "PARTS_PER_BILLION"],
        ["so2_ppb", "SO2", "SO2", "PARTS_PER_BILLION"],
        ["co_ppb", "CO", "CO", "PARTS_PER_BILLION"],
      ], rangeLabel, "Outside gas AQI history");
    }
  }

  // Weather isn't provider-dependent -- it comes from a separate feed
  // regardless of which provider is selected for pollutants -- so this
  // renders the same way either way. No severity bands exist anywhere in
  // this app for temp/humidity/pressure. Home only: the indoor half comes
  // from the sensor, which is physically tied to Home (the whole section is
  // hidden in Away mode, see applyModeVisibility).
  function renderWeatherChart(insidePoints, outsidePoints, rangeLabel) {
    const insideTemp = seriesFor(insidePoints, "temperature_c", null).points.map((p) => ({ t: p.t, v: displayTemp(p.v) }));
    const outsideTemp = seriesFor(outsidePoints, "temperature_c", null).points.map((p) => ({ t: p.t, v: displayTemp(p.v) }));
    renderOverlayRowChart(document.getElementById("chart-inside-outside-weather"), [
      {
        label: "Temp", unit: tempUnitLabel(), decimals: 1,
        inside: { points: insideTemp, bandFor: () => null },
        outside: { points: outsideTemp, bandFor: () => null },
      },
      {
        label: "Humidity", unit: "%", decimals: 1,
        inside: { points: seriesFor(insidePoints, "humidity_pct", null).points, bandFor: () => null },
        outside: { points: seriesFor(outsidePoints, "humidity_pct", null).points, bandFor: () => null },
      },
      {
        label: "Pressure", unit: " hPa", decimals: 1,
        inside: { points: seriesFor(insidePoints, "pressure_hpa", null).points, bandFor: () => null },
        outside: { points: seriesFor(outsidePoints, "pressure_hpa", null).points, bandFor: () => null },
      },
    ], { leftLabel: rangeLabel, label: "Inside vs outside temperature, humidity, pressure history" });
  }

  // Indoor air is physically tied to Home -- in Away mode, hide the
  // Temperature/humidity/pressure section at the bottom of the page
  // (comparing the sensor to a remote location would be misleading) and the
  // "faded = inside" legend on the AQI card, whose inside overlay isn't
  // drawn in Away either (see renderOutsideCharts).
  function applyModeVisibility() {
    const away = currentMode() === "away";
    const weatherEl = document.getElementById("section-inside-outside-weather");
    if (weatherEl) weatherEl.hidden = away;
    const legendEl = document.getElementById("aqi-legend");
    if (legendEl) legendEl.hidden = away;
  }

  // Readout=AQI (the default) is a non-technical read: every pollutant
  // normalized onto the shared 0-500 scale, computed from the concentration
  // when the provider gave only that. Anything that can't convert -- NH3 (no
  // EPA breakpoint) -- is dropped, same as the Basic dashboard and Forecast.
  // Readout=Units switches to the provider's own concentration (µg/m³/ppb),
  // falling back to AQI only for providers that report no concentration at
  // all (AirNow) -- same pattern as forecast.js's pollutantsHtml.
  function pollutantFactorsHtml(pollutants) {
    const units = readoutMode() === "units";
    return (pollutants || []).map((p) => {
      let valueHtml;
      let band = null;
      if (units) {
        if (typeof p.concentration_value === "number") {
          valueHtml = `${p.concentration_value}<span class="op-unit">${formatConcentrationUnits(p.concentration_units)}</span>`;
          band = bandForConcentration(p.parameter, p.concentration_value, p.concentration_units);
        } else if (typeof p.aqi === "number") {
          valueHtml = String(p.aqi);
          band = bandFromAqi(p.aqi);
        } else {
          valueHtml = "—";
        }
      } else {
        const aqi = typeof p.aqi === "number" ? p.aqi
          : (typeof p.concentration_value === "number" ? aqiFromConcentration(p.parameter, p.concentration_value, p.concentration_units) : null);
        if (typeof aqi !== "number") return null;
        valueHtml = String(aqi);
        band = bandFromAqi(aqi);
      }
      const colorStyle = band ? ` style="color: ${bandVar(band)}"` : "";
      return `<span class="outside-pollutant">${escapeHtml(p.parameter)}<span class="op-value"${colorStyle}>${valueHtml}</span></span>`;
    }).filter(Boolean).join("");
  }

  // Google's per-population-group guidance -- its equivalent of AirNow's
  // forecaster discussion, just structured differently (no narrative,
  // tailored text per group instead). Children get their own always-visible
  // line, same as general population. Everyone else (elderly, lung
  // disease, heart disease, athletes, pregnant) stays behind a toggle.
  // Moved here from dashboard.js -- this is the details page, so the
  // commentary lives here instead of on the one-screen overview.
  const HEALTH_GROUP_LABELS = {
    elderly: "Elderly",
    lungDiseasePopulation: "Lung disease",
    heartDiseasePopulation: "Heart disease",
    athletes: "Athletes",
    pregnantWomen: "Pregnant",
  };

  function healthRecommendationsHtml(hr) {
    if (!hr || !hr.generalPopulation) return "";
    const groups = Object.entries(HEALTH_GROUP_LABELS)
      .filter(([key]) => hr[key])
      .map(([key, label]) => `<p><strong>${label}:</strong> ${escapeHtml(hr[key])}</p>`)
      .join("");
    const childrenP = hr.children ? `<p><strong>Children:</strong> ${escapeHtml(hr.children)}</p>` : "";
    return `<div class="health-guidance">
      <p class="health-guidance-text">${escapeHtml(hr.generalPopulation)}</p>
      ${childrenP}
      ${groups ? `<button type="button" class="health-guidance-toggle" aria-expanded="false">Guidance for sensitive groups</button>
      <div class="health-guidance-groups" hidden>${groups}</div>` : ""}
    </div>`;
  }

  function outsideDiscussionHtml(d) {
    if (currentProvider() === "google") return healthRecommendationsHtml(d.health_recommendations);
    if (!d.discussion) return "";
    return `<div class="forecast-discussion">
      <button type="button" class="discussion-toggle" aria-expanded="false">Forecaster's discussion</button>
      <p class="discussion-text" hidden>${escapeHtml(d.discussion)}</p>
    </div>`;
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".discussion-toggle, .health-guidance-toggle");
    if (!btn) return;
    const body = btn.nextElementSibling;
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    body.hidden = expanded;
  });

  /* ---------- outside current reading ---------- */
  async function loadOutside() {
    // Kept outside the try/catch so the catch block can show the API's own
    // reason (e.g. "no healthy PurpleAir sensor nearby") instead of a bare
    // "Unavailable" when it has one -- same as dashboard.js.
    let apiErrorMsg = null;
    try {
      const res = await fetch(`/api/outside?provider=${currentProvider()}&mode=${currentMode()}`);
      const d = await res.json();
      if (!res.ok) { apiErrorMsg = d.error || "request failed"; throw new Error(apiErrorMsg); }

      const band = d.band;
      const whenText = d.time ? timeAgo(d.time) : formatObservedHour(d.observed_hour);

      document.getElementById("outside-aqi-tech").textContent = d.aqi ?? "—";
      document.getElementById("outside-aqi-tech").style.setProperty("--band-color", bandVar(band));
      document.getElementById("outside-category-tech").textContent = d.category || "—";
      document.getElementById("outside-dominant-tech").textContent = d.dominant_pollutant ? `Driven by ${d.dominant_pollutant}` : "";
      document.getElementById("outside-area-tech").textContent = d.reporting_area || "—";
      // Which provider this reading is from and when it was last refreshed --
      // both in one place, since the persistent chip bar's highlight alone
      // wasn't a clear enough tell of the current selection.
      document.getElementById("outside-updated-tech").textContent = `via ${providerLabel()} · Updated ${whenText}`;
      document.getElementById("outside-tech-card").style.setProperty("--edge-color", bandVar(band));
      document.getElementById("outside-pollutants").innerHTML = pollutantFactorsHtml(d.pollutants);
      document.getElementById("outside-discussion").innerHTML = outsideDiscussionHtml(d);
    } catch (e) {
      document.getElementById("outside-aqi-tech").textContent = "—";
      document.getElementById("outside-category-tech").textContent = apiErrorMsg || "Unavailable";
      document.getElementById("outside-dominant-tech").textContent = "";
      document.getElementById("outside-pollutants").innerHTML = "";
      document.getElementById("outside-discussion").innerHTML = "";
    }
  }

  /* ---------- history / charts ---------- */
  function rangeLabelFor(hours) {
    return { 6: "6h ago", 24: "24h ago", 72: "3d ago", 168: "7d ago" }[hours] || `${hours}h ago`;
  }

  // Charts measure their container's real width at render time (see
  // measureWidth), so a viewport change needs a re-render at the new width.
  // Caching the last-fetched points lets that happen instantly on resize
  // without a network round-trip.
  let lastOutsideSectionInsidePoints = null, lastOutsidePoints = null, lastOutsideRangeLabel = "";

  async function loadOutsideHistorySection(hours) {
    const mode = currentMode();
    const outsideUrl = `/api/outside/history?hours=${hours}&provider=${currentProvider()}&mode=${mode}`;
    // Away has no indoor reading to compare against -- skip the inside fetch
    // entirely rather than pulling Home's sensor history just to discard it.
    const fetches = mode === "away"
      ? [Promise.resolve(null), fetch(outsideUrl)]
      : [fetch(`/api/history?hours=${hours}`), fetch(outsideUrl)];
    const [insideRes, outsideRes] = await Promise.allSettled(fetches);
    const insidePoints = insideRes.status === "fulfilled" && insideRes.value && insideRes.value.ok ? await insideRes.value.json() : [];
    const outsidePoints = outsideRes.status === "fulfilled" && outsideRes.value.ok ? await outsideRes.value.json() : [];
    lastOutsideSectionInsidePoints = insidePoints;
    lastOutsidePoints = outsidePoints;
    lastOutsideRangeLabel = rangeLabelFor(hours);
    renderOutsideCharts(insidePoints, outsidePoints, lastOutsideRangeLabel);
    if (mode !== "away") renderWeatherChart(insidePoints, outsidePoints, lastOutsideRangeLabel);
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (lastOutsidePoints) {
        renderOutsideCharts(lastOutsideSectionInsidePoints, lastOutsidePoints, lastOutsideRangeLabel);
        if (currentMode() !== "away") renderWeatherChart(lastOutsideSectionInsidePoints, lastOutsidePoints, lastOutsideRangeLabel);
      }
    }, 200);
  });

  /* ---------- range toggle ---------- */
  let currentRangeOutside = 24;
  document.querySelectorAll("#range-toggle-outside button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#range-toggle-outside button").forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
      currentRangeOutside = Number(btn.getAttribute("data-range"));
      loadOutsideHistorySection(currentRangeOutside);
    });
  });

  // The header's Home/Away rail (common.js) flips this whole page over to
  // the other location's data.
  document.addEventListener("modechange", () => {
    applyModeVisibility();
    loadOutside();
    loadOutsideHistorySection(currentRangeOutside);
  });

  // The persistent provider bar (common.js) is now reachable from this page
  // too, not just Overview.
  document.addEventListener("providerchange", () => {
    loadOutside();
    loadOutsideHistorySection(currentRangeOutside);
  });

  // Settings panel's AQI/Units toggle (common.js) -- re-render everything
  // that reads readoutMode() at its new setting.
  document.addEventListener("readoutchange", () => {
    loadOutside();
    loadOutsideHistorySection(currentRangeOutside);
  });

  /* ---------- init ---------- */
  renderUnitToggle();
  applyModeVisibility();
  updateForecastLink();
  fetchAwayLoc().then(updateForecastLink);
  // Gate the first paint on the device's band table, as the other pages do.
  // It matters more here: this page's outside poll is 15 minutes, so painting
  // uncoloured and letting the next tick fix it would leave the cards grey for
  // a quarter of an hour.
  bandTableReady.then(() => {
    loadOutside();
    loadOutsideHistorySection(currentRangeOutside);
  });
  pollInterval(loadOutside, 15 * 60000);
  pollInterval(() => { loadOutsideHistorySection(currentRangeOutside); }, 60000);
})();
