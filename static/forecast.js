(function () {
  "use strict";

  // escapeHtml / bandVar / formatConcentrationUnits / readoutMode come from
  // common.js; bandFromAqi / aqiFromConcentration / bandForConcentration from
  // aqi.js (both loaded first). Theme toggle, readout toggle, settings panel,
  // and clock self-init in common.js.

  // AirNow's category names collapse onto the same 4-band scale as its AQI
  // numbers -- useful for the rows where AQI is -1 (not computed) and
  // category text is all that's available.
  const CATEGORY_TO_BAND = {
    "Good": "good",
    "Moderate": "fair",
    "Unhealthy for Sensitive Groups": "poor",
    "Unhealthy": "bad",
    "Very Unhealthy": "bad",
    "Hazardous": "bad",
  };

  function pollutantItemHtml(parameter, valueHtml, band) {
    const colorStyle = band ? ` style="color: ${bandVar(band)}"` : "";
    return `<span class="fd-pollutant-item"><span class="fp-label">${escapeHtml(parameter)}</span><span class="fp-value"${colorStyle}>${valueHtml}</span></span>`;
  }

  function pollutantsHtml(pollutants) {
    const units = readoutMode() === "units";
    return (pollutants || []).map((p) => {
      if (units) {
        // Engineering read: the provider's own AQI if it has one, else the raw
        // concentration, else the category text. AirNow sometimes doesn't
        // compute a per-pollutant AQI for forecast rows (AQI: -1, common during
        // an active alert) but still gives a category -- fall back to that
        // rather than a bare dash.
        if (typeof p.aqi === "number") return pollutantItemHtml(p.parameter, String(p.aqi), bandFromAqi(p.aqi));
        if (typeof p.concentration_value === "number") {
          return pollutantItemHtml(p.parameter,
            `${p.concentration_value} ${formatConcentrationUnits(p.concentration_units)}`,
            bandForConcentration(p.parameter, p.concentration_value, p.concentration_units));
        }
        return pollutantItemHtml(p.parameter, escapeHtml(p.category || "—"), CATEGORY_TO_BAND[p.category] || null);
      }
      // AQI read (the default): normalize every pollutant onto the 0-500 scale,
      // computing it from the concentration when the provider gave only that
      // (Google/OWM forecasts). Anything that can't be converted -- NH3 (no EPA
      // breakpoint), or an alert row with neither an AQI nor a concentration --
      // is hidden, so the family view stays to the one comparable number.
      const aqi = typeof p.aqi === "number" ? p.aqi
        : (typeof p.concentration_value === "number" ? aqiFromConcentration(p.parameter, p.concentration_value, p.concentration_units) : null);
      return typeof aqi === "number" ? pollutantItemHtml(p.parameter, String(aqi), bandFromAqi(aqi)) : null;
    }).filter(Boolean).join("");
  }

  // Google's per-population-group guidance -- more specific than AirNow's one
  // paragraph for everyone. Children come first: kids react differently to air
  // quality and it's the group most likely to matter for this household, so
  // their line sits in front of the toggle in the expanded layout; everyone
  // else (elderly, lung disease, heart disease, athletes, pregnant) is behind
  // it.
  const HEALTH_GROUP_LABELS = {
    children: "Children",
    elderly: "Elderly",
    lungDiseasePopulation: "Lung disease",
    heartDiseasePopulation: "Heart disease",
    athletes: "Athletes",
    pregnantWomen: "Pregnant",
  };

  // Within a single day Google hands back the identical sentence for several
  // groups at once, and often reuses the general-population sentence verbatim
  // as well. Merge groups that share wording onto one line, and drop the ones
  // that only restate the general advice (which already leads the block), so
  // what's left is the per-group differences rather than six near-copies.
  function healthGroupLines(hr) {
    const byText = new Map();
    for (const [key, label] of Object.entries(HEALTH_GROUP_LABELS)) {
      const text = hr[key];
      if (!text || text === hr.generalPopulation) continue;
      if (!byText.has(text)) byText.set(text, []);
      byText.get(text).push(label);
    }
    return Array.from(byText, ([text, labels]) => ({ labels, text }));
  }

  function healthGroupLineHtml({ labels, text }) {
    return `<p><strong>${labels.join(", ")}:</strong> ${escapeHtml(text)}</p>`;
  }

  // collapsed: fold the whole block behind one toggle. Used in the day cards,
  // where several paragraphs per card -- boilerplate keyed to the AQI category,
  // largely the same from day to day -- buried the numbers people came for. The
  // shared block above the cards appears only once, so it stays open with just
  // the sensitive-group lines folded away.
  function healthRecommendationsHtml(hr, collapsed) {
    if (!hr || !hr.generalPopulation) return "";
    const general = `<p class="health-guidance-text">${escapeHtml(hr.generalPopulation)}</p>`;
    const lines = healthGroupLines(hr);
    if (collapsed) {
      return `<div class="health-guidance">
        <button type="button" class="health-guidance-toggle" aria-expanded="false">Health advice</button>
        <div class="health-guidance-groups" hidden>${general}${lines.map(healthGroupLineHtml).join("")}</div>
      </div>`;
    }
    // Children lead when Google gave them advice that differs from the general
    // paragraph (HEALTH_GROUP_LABELS order puts them first); rest go behind the
    // toggle.
    const lead = lines.length && lines[0].labels[0] === "Children"
      ? healthGroupLineHtml(lines.shift()) : "";
    return `<div class="health-guidance">
      ${general}
      ${lead}
      ${lines.length ? `<button type="button" class="health-guidance-toggle" aria-expanded="false">Guidance for sensitive groups</button>
      <div class="health-guidance-groups" hidden>${lines.map(healthGroupLineHtml).join("")}</div>` : ""}
    </div>`;
  }

  // Google's health guidance is generated per day but in practice is
  // boilerplate keyed to the AQI category, not the specific day -- when a
  // forecast run has the same category several days running (common), every
  // card repeated the exact same paragraph. Detect that and show it once
  // instead, only falling back to per-day text when it actually differs.
  function allHealthRecommendationsEqual(days) {
    if (!days.length || !days.every((d) => d.health_recommendations && d.health_recommendations.generalPopulation)) return false;
    const first = JSON.stringify(days[0].health_recommendations);
    return days.every((d) => JSON.stringify(d.health_recommendations) === first);
  }

  function dayLabel(dateStr) {
    const d = new Date(dateStr + "T00:00:00");
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const diffDays = Math.round((d - today) / 86400000);
    if (diffDays === 0) return "Today";
    if (diffDays === 1) return "Tomorrow";
    return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
  }

  function toast(msg) {
    const stack = document.getElementById("toast-stack");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  // The provider is chosen from the persistent chip bar (common.js,
  // currentProvider()) -- this page reacts to it via "providerchange" below
  // and shows which agency actually served the data (forecast-source, using
  // the response's own "provider" field, which is the real source of truth --
  // see loadForecast).
  const PROVIDER_LABELS = { google: "Google Air Quality", openweathermap: "OpenWeatherMap", airnow: "AirNow", purpleair: "PurpleAir" };
  function providerLabel(provider) {
    return PROVIDER_LABELS[provider || currentProvider()] || "AirNow";
  }

  // null = home; a zip when Away is active. Driven entirely by the header's
  // Home/Away rail -- Forecast has no location picker of its own (currentMode/
  // getAwayLoc/fetchAwayLoc come from common.js).
  let selectedZip = null;

  async function applyMode() {
    const awayLoc = currentMode() === "away" ? await fetchAwayLoc() : null;
    selectedZip = awayLoc ? awayLoc.zip : null;
  }

  // Held so the AQI/Units readout toggle re-renders the day cards in place,
  // without a refetch (which for forecasts could hit the upstream API).
  let lastForecast = null;

  function renderForecastDays(d) {
    const daysEl = document.getElementById("forecast-days");
    const sharedGuidanceEl = document.getElementById("forecast-health-guidance");
    if (!d.days || !d.days.length) {
      daysEl.innerHTML = '<div class="empty-state">No forecast for this location.</div>';
      sharedGuidanceEl.hidden = true;
      sharedGuidanceEl.innerHTML = "";
      return;
    }
    const guidanceIsShared = allHealthRecommendationsEqual(d.days);
    if (guidanceIsShared) {
      sharedGuidanceEl.innerHTML = healthRecommendationsHtml(d.days[0].health_recommendations);
      sharedGuidanceEl.hidden = false;
    } else {
      sharedGuidanceEl.hidden = true;
      sharedGuidanceEl.innerHTML = "";
    }
    daysEl.innerHTML = d.days.map((day) => {
      // The day headline is the AQI number when there is one (there's no
      // single concentration for a whole day); the readout toggle only affects
      // the per-pollutant breakdown below.
      // AirNow routinely forecasts a category without a number -- AQI: -1 on
      // every row, which airnow.py maps to null (see its Category.Number
      // fallback). Rendering "AQI —" for that put a placeholder dash directly
      // under a perfectly good "Moderate", which reads as a value that failed
      // to load rather than one the agency never issued. The category badge
      // already carries the day, so the line just goes.
      const aqiHtml = day.aqi != null ? `<div class="fd-aqi">AQI ${day.aqi}</div>` : "";
      // Render the breakdown first: in AQI mode it can come back empty (every
      // pollutant filtered out as non-convertible), in which case fall back to
      // just naming the dominant pollutant.
      const pollutantsInner = pollutantsHtml(day.pollutants);
      // "Driven by O3", not a bare "O3" -- same phrasing the Overview racks and
      // the Outdoor headline use for the dominant pollutant, and on a card with
      // no AQI line left it would otherwise be a lone unexplained token.
      const pollutantsBlock = pollutantsInner
        ? `<div class="fd-pollutants">${pollutantsInner}</div>`
        : (day.dominant_pollutant
          ? `<div class="fd-pollutant">Driven by ${escapeHtml(day.dominant_pollutant)}</div>`
          : "");
      // Only add a separate "driven by" line when the full breakdown is also
      // shown -- otherwise it'd just repeat the fallback text above.
      const dominantHtml = pollutantsInner && day.dominant_pollutant
        ? `<div class="fd-dominant">Driven by ${escapeHtml(day.dominant_pollutant)}</div>`
        : "";
      const actionBadge = day.action_day ? '<div class="fd-action-badge">Action Day</div>' : "";
      return `<div class="forecast-day">
        <div class="fd-label">${dayLabel(day.date)}</div>
        ${actionBadge}
        <div class="fd-badge" style="--band-color: ${bandVar(day.band)}">${escapeHtml(day.category)}</div>
        ${aqiHtml}
        ${dominantHtml}
        ${pollutantsBlock}
        ${guidanceIsShared ? "" : healthRecommendationsHtml(day.health_recommendations, true)}
      </div>`;
    }).join("");
  }

  // Re-render the day cards when AQI/Units is toggled (common.js persists it
  // and fires this); no refetch needed.
  document.addEventListener("readoutchange", () => {
    if (lastForecast) renderForecastDays(lastForecast);
  });

  async function loadForecast(force) {
    const daysEl = document.getElementById("forecast-days");
    const areaEl = document.getElementById("forecast-area");
    const sourceEl = document.getElementById("forecast-source");
    const discussionWrap = document.getElementById("forecast-discussion");
    const discussionText = document.getElementById("discussion-text");
    const discussionToggle = document.getElementById("discussion-toggle");

    const zipParam = selectedZip ? `zip=${encodeURIComponent(selectedZip)}&` : "";
    const refreshParam = force ? "&refresh=1" : "";
    const url = `/api/forecast?${zipParam}provider=${currentProvider()}${refreshParam}`;
    try {
      const res = await fetch(url);
      let d;
      try {
        d = await res.json();
      } catch (parseErr) {
        // A non-JSON body (a proxy/timeout error page, e.g.) would otherwise
        // surface as a raw "Unexpected token '<'..." parser error.
        throw new Error(`unexpected response (${res.status})`);
      }
      if (!res.ok) throw new Error(d.error || "request failed");

      // The response's own "provider" field is the source of truth for what
      // actually served this data (not just currentProvider, which could
      // theoretically be stale across tabs) -- always show which agency/
      // model the forecast on screen came from, same principle as the main
      // dashboard's provider chips. fetched_at is when the forecast was
      // actually pulled from the upstream API, not just when this page asked
      // for it -- forecasts are cached for hours, so "just now" would be
      // misleading most of the time.
      sourceEl.textContent = d.fetched_at
        ? `via ${providerLabel(d.provider)} · Updated ${timeAgo(d.fetched_at)}`
        : `via ${providerLabel(d.provider)}`;
      areaEl.textContent = d.reporting_area || "—";
      lastForecast = d;
      renderForecastDays(d);

      if (d.discussion) {
        discussionWrap.hidden = false;
        discussionText.textContent = d.discussion;
        discussionToggle.setAttribute("aria-expanded", "false");
        discussionText.hidden = true;
      } else {
        discussionWrap.hidden = true;
      }
    } catch (e) {
      sourceEl.textContent = `via ${providerLabel()}`;
      areaEl.textContent = "—";
      daysEl.innerHTML = `<div class="empty-state">Couldn't reach ${providerLabel()} — ${escapeHtml(e.message)}</div>`;
      discussionWrap.hidden = true;
      document.getElementById("forecast-health-guidance").hidden = true;
    }
  }

  document.getElementById("forecast-refresh").addEventListener("click", async () => {
    const btn = document.getElementById("forecast-refresh");
    btn.disabled = true;
    btn.textContent = "Refreshing…";
    await loadForecast(true);
    btn.disabled = false;
    btn.textContent = "Refresh";
    toast("Forecast refreshed");
  });

  document.getElementById("discussion-toggle").addEventListener("click", () => {
    const btn = document.getElementById("discussion-toggle");
    const p = document.getElementById("discussion-text");
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    p.hidden = expanded;
  });

  // Day cards are rebuilt on every loadForecast(), so this is delegated
  // rather than bound to specific elements that won't exist yet.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".health-guidance-toggle");
    if (!btn) return;
    const groups = btn.nextElementSibling;
    const expanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!expanded));
    groups.hidden = expanded;
  });

  // The header's Home/Away rail (common.js) jumps this page to the other
  // location too, same as the dashboard and Technical.
  document.addEventListener("modechange", async () => {
    await applyMode();
    loadForecast();
  });

  // The persistent provider bar (common.js) is now reachable from this page
  // too, not just Overview.
  document.addEventListener("providerchange", () => loadForecast());

  document.getElementById("forecast-source").textContent = `via ${providerLabel()}`;
  // Band table before the first render, same as the other pages -- the AQI
  // cuts that colour a forecast day's pollutants come from the device now, and
  // this page only re-polls every 15 minutes.
  Promise.all([applyMode(), bandTableReady]).then(loadForecast);
  pollInterval(loadForecast, 15 * 60000);
})();
