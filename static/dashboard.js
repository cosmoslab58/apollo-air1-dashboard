(function () {
  "use strict";

  // fmt / timeAgo / escapeHtml / bandVar / formatConcentrationUnits /
  // bandFromCo2 / bandForVocIndex / seriesFor / readoutMode and the provider
  // constants come from common.js; bandFromAqi / bandForConcentration /
  // aqiFromConcentration from aqi.js (both loaded first). Theme toggle, readout
  // toggle, settings panel, clock, and SW registration self-init in common.js.

  // No temperature-unit (°F/°C) handling here, unlike indoor.js/technical.js:
  // Overview renders no temperature at all, so index.html hides that settings
  // row (show_units_row) and this page has nothing to convert or re-render.

  // Band ranking and the inside worst-of both come from common.js now
  // (worseBandName / worstBandOf), so this page and the LED rank severity the
  // same way. worseBand stays as a thin alias because the Outside cards use it
  // too and their bands come from a different source (provider AQI).
  const worseBand = worseBandName;

  // Six labels for the firmware's six bands, describing the READING rather than
  // passing a health verdict, because this headline is a worst-of that any of
  // four channels can drive.
  //
  // "Good / Fair / Poor / Bad" was the old wording and it does not survive that
  // fact. Two problems. It is a near-paraphrase of EPA's AQI categories, which
  // are legally defined terms for outdoor criteria pollutants -- the firmware
  // refuses to name its bands for exactly this reason (see its LED section). And
  // the headline is frequently driven by the VOC index, which is a deviation
  // from this room's own recent baseline, not a concentration: calling that
  // "Bad" asserts a health claim the measurement cannot support.
  //
  // "Elevated / High / Very high" say how far from normal the worst channel is,
  // which is exactly what the number means and is true of all four channels.
  // Paired with the "Driven by X" sub-line, the user gets magnitude and cause
  // without the page inventing authority.
  //
  // NOTE: intentionally not aligned with the BAND_VARS css names (good/fair/
  // .../hazard). Those are style hooks, some used outside band context
  // (--good colours a downward trend arrow), so renaming them would be churn
  // for no gain. These strings are the user-facing vocabulary.
  function bandLabel(band) {
    return {
      good: "Normal", fair: "Elevated", poor: "High",
      bad: "Very high", severe: "Severe", hazard: "Extreme",
    }[band] || null;
  }

  /* ---------- mini sparkline (rack-spark) ----------
   * Deliberately simpler than Technical's charts: one flat color (the
   * current band), no axis/grid/labels -- at 84x34px those would just be
   * noise. Just enough to show "trending up/down/flat" at a glance. */
  function renderMiniSpark(el, points, band) {
    if (!el) return;
    // hidden, not just emptied: below 260px of column the sparkline is a
    // full-width 36px row of its own, so an Away location (no history to
    // draw) left a dead band inside the Outside card that read as a chart
    // that had failed to render. Nothing to show, so it takes no space.
    if (!points || points.length === 0) { el.innerHTML = ""; el.hidden = true; return; }
    el.hidden = false;
    const w = el.clientWidth || 84, h = el.clientHeight || 34;
    // A single sample (common for the sparse AirNow feed in a short window)
    // can't draw a trend line -- show a centered dot so the tile reads as
    // "one reading so far", not "broken/blank".
    if (points.length === 1) {
      const c = bandVar(band);
      el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-hidden="true">
        <circle cx="${(w / 2).toFixed(1)}" cy="${(h / 2).toFixed(1)}" r="3" fill="${c}" /></svg>`;
      return;
    }
    const vals = points.map((p) => p.v);
    const vMin = Math.min(...vals), vMax = Math.max(...vals);
    const pad = (vMax - vMin) * 0.15 || 1;
    const lo = vMin - pad, hi = vMax + pad;
    const tMin = points[0].t, tMax = points[points.length - 1].t;
    // Drawing area inset by half the stroke width plus the round cap's reach,
    // rather than the full 0..w/0..h box. A path drawn to the box edge has
    // half its stroke outside the viewBox, and the rack clips it (overflow:
    // hidden for the rounded corners) -- the flat top on a spike that reaches
    // the series max, and the shaved first/last sample, both read as a chart
    // cropped by mistake rather than one scaled to fit.
    const inset = 1.5;
    const x0 = inset, x1 = Math.max(inset, w - inset);
    const y0 = inset, y1 = Math.max(inset, h - inset);
    const xAt = (t) => x0 + ((t - tMin) / (tMax - tMin || 1)) * (x1 - x0);
    const yAt = (v) => y1 - ((v - lo) / (hi - lo || 1)) * (y1 - y0);
    const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${xAt(p.t).toFixed(1)},${yAt(p.v).toFixed(1)}`).join(" ");
    const color = bandVar(band);
    // The fill closes on the baseline the line is drawn against, not the
    // viewBox floor, so its bottom edge and the stroke's lowest point agree.
    const areaD = `${d} L${x1.toFixed(1)},${y1.toFixed(1)} L${x0.toFixed(1)},${y1.toFixed(1)} Z`;
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-hidden="true">
      <path d="${areaD}" fill="${color}" opacity="0.14" stroke="none" />
      <path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
    </svg>`;
  }

  /* ---------- provider switch (AirNow / Google / PurpleAir / OpenWeatherMap) ----------
   * The chip bar itself, currentProvider(), and the shared "modechange"
   * re-fetch all live in common.js now (the persistent bar is the same
   * control on Overview/Outdoor/Forecast, not just this page). This page
   * only needs to react to a provider actually changing. */
  function providerLabel() {
    return PROVIDER_NAMES[currentProvider()] || "AirNow";
  }

  document.addEventListener("providerchange", () => {
    loadOutside();
    loadBasicSparks();
  });

  // The header's Home/Away rail (common.js) flips the whole outside half of
  // this page over to the other location's data -- the provider choice
  // itself doesn't change, just what it's fetched for.
  document.addEventListener("modechange", () => {
    loadOutside();
    loadBasicSparks();
  });

  // One row per metric instead of a badge grid -- the rack-rows list.
  // --rr-color is a CSS custom prop the .rr-value rule already reads, so an
  // unset (null) band just falls back to the row's default ink color rather
  // than needing a conditional class per row.
  function rackRow(label, valueHtml, band) {
    const style = band ? ` style="--rr-color: ${bandVar(band)}"` : "";
    return `<div class="rack-row"${style}><span class="rr-label">${escapeHtml(label)}</span><span class="rr-value">${valueHtml}</span></div>`;
  }

  // PM2.5 leads (and, under Readout=AQI, reads on the same 0-500 scale as
  // Outside's own first row) so it lands on the same row as Outside's PM2.5
  // -- the one pollutant both racks share -- letting a glance across the two
  // columns compare them directly instead of hunting for the matching label.
  // CO2 and VOC follow -- along with PM2.5, they're the two other signals
  // that actually drive severity here (all four graded channels feed the
  // headline band via worstBandOf; NOx is the one omitted, being effectively
  // pinned at band 0 on this unit). Trimmed down from a
  // 10-field rack (PM1.0/PM4.0/NOx/Temp/Humidity/Pressure included) to match
  // Outside's own density -- Outside never showed more than its handful of
  // pollutants either, no weather/comfort metrics, full breakdown one tap
  // away on the Indoor details page or Grafana. PM10 stays despite not
  // driving the band: it's the other half of the two EPA particulate sizes
  // everyone recognizes, same reasoning Outside keeps it for.
  function insideRowsHtml(d) {
    const units = readoutMode() === "units";
    const pmRow = (parameter, raw) => {
      const band = bandForConcentration(parameter, raw, "MICROGRAMS_PER_CUBIC_METER");
      return units
        ? { label: parameter, value: raw, decimals: 1, unit: "µg/m³", band }
        : { label: parameter, value: aqiFromConcentration(parameter, raw, "MICROGRAMS_PER_CUBIC_METER"), decimals: 0, unit: "", band };
    };
    const items = [
      pmRow("PM2.5", d.pm2_5_ugm3),
      pmRow("PM10", d.pm10_0_ugm3),
      { label: "CO2", value: d.co2_ppm, decimals: 0, unit: "ppm", band: bandFromCo2(d.co2_ppm) },
      { label: "VOC", value: d.voc_index, decimals: 0, unit: "", band: bandForVocIndex(d.voc_index) },
    ];
    return items.map((it) => {
      const valueHtml = typeof it.value === "number"
        ? `${fmt(it.value, it.decimals)}${it.unit ? `<span class="rr-unit">${it.unit}</span>` : ""}`
        : "—";
      return rackRow(it.label, valueHtml, it.band);
    }).join("");
  }

  // Readout=AQI (the default) puts every provider on one comparable scale --
  // each pollutant's AQI. Anything that can't be put on that scale (OWM's NH3,
  // which has no EPA breakpoint) is dropped here; it reappears under
  // Readout=Units, which shows the provider's reported concentration instead,
  // falling back to AQI for AirNow (which reports no concentration). The AQI is
  // derived upstream (Node-RED) and read per pollutant; the concentration->AQI
  // fallback only fills a gap for older points stored without a per-pollutant
  // AQI -- the app does no primary AQI math.
  function outsideRowsHtml(pollutants) {
    const units = readoutMode() === "units";
    return (pollutants || []).map((p) => {
      if (units) {
        if (typeof p.concentration_value === "number") {
          const valueHtml = `${p.concentration_value}<span class="rr-unit">${formatConcentrationUnits(p.concentration_units)}</span>`;
          return rackRow(p.parameter, valueHtml, bandForConcentration(p.parameter, p.concentration_value, p.concentration_units));
        }
        return typeof p.aqi === "number" ? rackRow(p.parameter, String(p.aqi), bandFromAqi(p.aqi)) : null;
      }
      const aqi = typeof p.aqi === "number" ? p.aqi
        : (typeof p.concentration_value === "number" ? aqiFromConcentration(p.parameter, p.concentration_value, p.concentration_units) : null);
      return typeof aqi === "number" ? rackRow(p.parameter, String(aqi), bandFromAqi(aqi)) : null;
    }).filter(Boolean).join("");
  }

  /* ---------- outside (AirNow / Google / PurpleAir / OpenWeatherMap) ---------- */
  // Kept so the AQI/Units readout toggle can re-render the rows without refetching.
  let lastOutsidePollutants = null;

  // Forecaster's discussion / Google health guidance live on the Outdoor
  // page now (technical.js), not here -- the dashboard is the at-a-glance
  // view and that commentary was the biggest thing standing between it and
  // fitting on one screen without scrolling.
  async function loadOutside() {
    // Kept outside the try/catch so the catch block can tell an API-reported
    // reason (e.g. "no healthy PurpleAir sensor nearby") apart from a genuine
    // network/parse failure, and show the real one instead of a generic line.
    let apiErrorMsg = null;
    try {
      const res = await fetch(`/api/outside?provider=${currentProvider()}&mode=${currentMode()}`);
      const d = await res.json();
      if (!res.ok) { apiErrorMsg = d.error || "request failed"; throw new Error(apiErrorMsg); }

      const band = d.band;
      const outAqi = document.getElementById("out-aqi");
      outAqi.textContent = typeof d.aqi === "number" ? String(d.aqi) : "—";
      outAqi.style.setProperty("--band-color", bandVar(band));
      document.getElementById("outside-area").textContent = d.reporting_area || "—";
      document.getElementById("out-category").textContent = d.category || "Loading…";
      document.getElementById("out-sub").textContent = d.dominant_pollutant ? `Driven by ${d.dominant_pollutant}` : "";
      lastOutsidePollutants = d.pollutants;
      document.getElementById("outside-rows").innerHTML = outsideRowsHtml(d.pollutants);
      // Which provider this reading is from and when it was last refreshed
      // into the DB -- both in one place, since the persistent chip bar's
      // highlight alone wasn't a clear enough tell of the current selection.
      // An Away location is fetched from the provider on demand rather than
      // polled into Influx, so it has no stored timestamp and no history --
      // which is why the sparkline is empty here too. Saying "live" is the
      // honest version of that: dropping silently to "via AirNow" next to
      // Inside's "Updated just now" read as a freshness stamp that had failed
      // to load, rather than a reading that was never logged in the first
      // place.
      document.getElementById("out-updated").textContent = d.time
        ? `via ${providerLabel()} · Updated ${timeAgo(d.time)}`
        : `via ${providerLabel()} · live`;
    } catch (e) {
      document.getElementById("out-aqi").textContent = "—";
      document.getElementById("out-category").textContent = apiErrorMsg || ("Couldn't reach " + providerLabel() + ".");
      document.getElementById("out-sub").textContent = "";
      lastOutsidePollutants = null;
      document.getElementById("outside-rows").innerHTML = "";
      document.getElementById("out-updated").textContent = "";
    }
  }

  // Re-render both racks' rows in place when the AQI/Units readout is
  // toggled (common.js persists the choice and fires this event); no
  // refetch needed. Inside's row order doesn't otherwise depend on this --
  // only PM2.5's own value/unit does (see insideRowsHtml).
  document.addEventListener("readoutchange", () => {
    if (lastOutsidePollutants) {
      document.getElementById("outside-rows").innerHTML = outsideRowsHtml(lastOutsidePollutants);
    }
    if (lastInsideLatest) {
      document.getElementById("inside-rows").innerHTML = insideRowsHtml(lastInsideLatest);
    }
  });

  // Basic view's compact trend lines -- a short (6h) fetch, independent of
  // Technical's own range control (a separate page now), so the
  // at-a-glance sparkline never jumps around based on state set elsewhere.
  async function loadBasicSparks() {
    try {
      const [insideRes, outsideRes] = await Promise.allSettled([
        fetch("/api/history?hours=6"),
        fetch(`/api/outside/history?hours=6&provider=${currentProvider()}&mode=${currentMode()}`),
      ]);
      const insidePoints = insideRes.status === "fulfilled" && insideRes.value.ok ? await insideRes.value.json() : [];
      const outsidePoints = outsideRes.status === "fulfilled" && outsideRes.value.ok ? await outsideRes.value.json() : [];
      const inSeries = seriesFor(insidePoints, "aqi", null).points;
      const outSeries = seriesFor(outsidePoints, "aqi", null).points;
      renderMiniSpark(document.getElementById("in-spark"), inSeries, inSeries.length ? bandFromAqi(inSeries[inSeries.length - 1].v) : null);
      renderMiniSpark(document.getElementById("out-spark"), outSeries, outSeries.length ? bandFromAqi(outSeries[outSeries.length - 1].v) : null);
    } catch (e) {
      // Decorative -- fine to leave the sparklines blank on failure.
    }
  }

  /* ---------- indoor latest reading ---------- */
  // Kept so the AQI/Units readout toggle can re-render PM2.5's row without
  // refetching -- same pattern as lastOutsidePollutants above.
  let lastInsideLatest = null;

  async function loadLatest() {
    try {
      const res = await fetch("/api/latest");
      if (res.status === 404) {
        setIndoorUnavailable("Waiting for the AIR-1 to report in.");
        return;
      }
      if (!res.ok) throw new Error("request failed");
      const d = await res.json();

      // Worst across all four graded channels, and the channel that got it
      // there -- the same comparison the device makes to colour its LED. The
      // previous version compared only AQI and CO2, so it could not name VOC,
      // which is in fact this unit's worst channel 94% of the time it is above
      // green, and it showed a colour the light disagreed with.
      const { band, label } = worstBandOf(d);
      const inAqi = document.getElementById("in-aqi");
      inAqi.textContent = typeof d.aqi === "number" ? String(Math.round(d.aqi)) : "—";
      inAqi.style.setProperty("--band-color", bandVar(band));
      document.getElementById("in-category").textContent = bandLabel(band) || "Waiting for a reading…";
      document.getElementById("in-sub").textContent = label ? `Driven by ${label}` : "";
      lastInsideLatest = d;
      document.getElementById("inside-rows").innerHTML = insideRowsHtml(d);
      // When the AIR-1 last reported a reading into the DB.
      document.getElementById("in-updated").textContent = d.time ? "Updated " + timeAgo(d.time) : "";
    } catch (e) {
      setIndoorUnavailable("Couldn't reach the sensor feed.");
    }
  }

  function setIndoorUnavailable(msg) {
    document.getElementById("in-category").textContent = "—";
    document.getElementById("in-sub").textContent = msg;
    const inAqi = document.getElementById("in-aqi");
    inAqi.textContent = "—";
    inAqi.style.setProperty("--band-color", "var(--ink-dim)");
    lastInsideLatest = null;
    document.getElementById("inside-rows").innerHTML = insideRowsHtml({});
    document.getElementById("in-updated").textContent = "";
  }

  /* ---------- init ---------- */
  updateForecastLink();
  fetchAwayLoc().then(updateForecastLink);
  // Wait for the device's band table before the first paint, so tiles come up
  // already coloured rather than grey-then-flash. bandTableReady never rejects
  // -- if no table ever arrives, these render uncoloured and stay that way,
  // which is the intended "we don't invent colours" behaviour.
  bandTableReady.then(() => {
    loadLatest();
    loadOutside();
    loadBasicSparks();
  });
  // The inside reading follows the device's own publish cadence (see
  // latestPollMs). Outside stays at 15 min -- those are hourly-ish upstream
  // feeds, so polling them faster returns the same numbers and spends someone
  // else's API quota doing it.
  watchLatest(loadLatest, () => latestPollMs(lastInsideLatest));
  pollInterval(() => { loadOutside(); loadBasicSparks(); }, 15 * 60000);
})();
