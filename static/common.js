// Shared browser helpers + self-initializing page chrome, loaded after aqi.js
// and before each page's own script (index/forecast/technical/indoor). Its
// top-level functions/consts are visible inside those scripts' IIFEs, the same
// way aqi.js's are. This is the single home for the formatting/band helpers and
// the theme/settings/clock UI that every page used to copy-paste verbatim.
//
// Deliberately NOT here: the temperature-unit toggle. Its click handler reloads
// different things on each page (loadLatest vs loadHistory vs the outside
// overlay) and renderUnitToggle updates page-specific elements, so that stays
// local to each page rather than forcing shared mutable unit state.

/* ---------- formatting ---------- */
function fmt(value, decimals) {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  return Number(value).toFixed(decimals);
}

// Accepts either an ISO timestamp string or epoch seconds (the MQTT seen_at
// values are epoch seconds; Influx times are ISO).
function timeAgo(isoOrEpochSeconds) {
  if (!isoOrEpochSeconds) return "—";
  const ms = typeof isoOrEpochSeconds === "number" ? isoOrEpochSeconds * 1000 : new Date(isoOrEpochSeconds).getTime();
  if (Number.isNaN(ms)) return "—";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// A band name -> the CSS custom property the .rr-value / --band-color rules
// already read; null bands fall back to the neutral dim ink.
function bandVar(band) {
  return band ? `var(--${band})` : "var(--ink-dim)";
}

// Google's own enum value, abbreviated to the unit symbol everyone reads at a
// glance. Falls back to a de-underscored lowercase form for anything unmapped.
function formatConcentrationUnits(units) {
  const short = { PARTS_PER_BILLION: "ppb", MICROGRAMS_PER_CUBIC_METER: "µg/m³" };
  return short[units] || (units || "").replace(/_/g, " ").toLowerCase();
}

/* ================= severity bands =================
 * THIS FILE DEFINES NO THRESHOLDS. Every cutoff comes from the AIR-1, which
 * publishes its band table to a retained MQTT topic that the server re-serves
 * at /api/bands. The device grades its own readings against that table, sets
 * the publish cadence from it, and picks its LED colour from it -- so reading
 * the same table here is what guarantees a CO2 tile on this page and the light
 * on the wall can never disagree.
 *
 * This used to be four hardcoded functions, and they HAD drifted: CO2 banded at
 * 1000/1500/2000 here versus 800/1100/2000/3500/5000 in the firmware, VOC had
 * no green band at all, and NOx was never coloured. A reading of 900ppm showed
 * green on this page while the LED was yellow.
 *
 * Until the table arrives, every band is null and readings render uncoloured.
 * That is deliberate: an invented colour is worse than no colour, because it is
 * indistinguishable from a real one.
 *
 * Band index -> CSS custom property. Six entries because the firmware grades
 * 0-5; the last two are new (see style.css). Names are this app's own words,
 * NOT the EPA category names -- those are legally defined for outdoor criteria
 * pollutants and would be borrowed authority on a CO2 or VOC reading. The
 * firmware refuses to name its bands for exactly that reason. */
const BAND_VARS = ["good", "fair", "poor", "bad", "severe", "hazard"];

let _bandTable = null;

// Resolves once the first fetch settles, so pages can await it before their
// initial render instead of painting uncoloured and then flashing into colour.
// Never rejects: a missing table is a supported state, not an error.
const bandTableReady = fetch("/api/bands")
  .then((r) => (r.ok ? r.json() : null))
  .then((t) => { _bandTable = t; return t; })
  .catch(() => null);

function bandTable() {
  return _bandTable;
}

/* Grade a value against one channel's cuts, returning a CSS var name.
 *
 * The comparison is upper-bound-INCLUSIVE (`value <= cut`), matching the
 * `compare: "lte"` the device states in the payload and the EPA breakpoint
 * style it follows (0-50 Good, 51-100 Moderate). Band index is the first cut
 * the value fits under, or cuts.length past the end -- identical to the
 * firmware's `grade()` lambda, which is the only other implementation. */
function bandForChannel(channel, value) {
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  const table = _bandTable;
  if (!table) return null;
  const cuts = table[channel];
  if (!Array.isArray(cuts) || cuts.length === 0) return null;
  let index = cuts.length;
  for (let i = 0; i < cuts.length; i++) {
    if (value <= cuts[i]) { index = i; break; }
  }
  return BAND_VARS[Math.min(index, BAND_VARS.length - 1)] || null;
}

function bandFromCo2(co2) { return bandForChannel("co2", co2); }
function bandForVocIndex(v) { return bandForChannel("voc", v); }
function bandForNoxIndex(v) { return bandForChannel("nox", v); }

// Rank two band names; null (no reading, or no table) loses to anything real.
// Compares by position in BAND_VARS, so adding a band above "bad" needs no
// change here.
function worseBandName(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  return BAND_VARS.indexOf(a) >= BAND_VARS.indexOf(b) ? a : b;
}

/* The worst band across every channel the device grades, and which channel got
 * it there. Used for the Overview headline and its "Driven by X" subtitle.
 *
 * All four channels, not just AQI and CO2 as the Overview used to compare: over
 * 2.5 days of this unit's data VOC was the worst channel 94% of the time it was
 * above green, so a headline built from AQI and CO2 alone was routinely naming
 * the wrong cause -- and disagreeing with an LED that does look at all four. */
const BAND_CHANNELS = [
  { channel: "aqi", key: "aqi", label: "PM2.5" },
  { channel: "co2", key: "co2_ppm", label: "CO2" },
  { channel: "voc", key: "voc_index", label: "VOC" },
  { channel: "nox", key: "nox_index", label: "NOx" },
];

function worstBandOf(latest) {
  let best = { band: null, label: null };
  if (!latest) return best;
  for (const c of BAND_CHANNELS) {
    const band = bandForChannel(c.channel, latest[c.key]);
    if (!band) continue;
    if (!best.band || BAND_VARS.indexOf(band) > BAND_VARS.indexOf(best.band)) {
      best = { band, label: c.label };
    }
  }
  return best;
}

/* ---------- provider identity (kept in one place so the pages and the
 * server's api_forecast set can't silently drift) ---------- */
const PROVIDER_NAMES = { airnow: "AirNow", google: "Google", purpleair: "PurpleAir", openweathermap: "OWM" };
const PROVIDER_ORDER = ["airnow", "google", "purpleair", "openweathermap"];
// PurpleAir is the only provider with no forecast: one real-time sensor, no
// forward-looking model. Its Forecast link would hand back a *different*
// provider's forecast, so it's hidden. Any provider not handled by the server's
// api_forecast belongs in this set.
const PROVIDERS_WITHOUT_FORECAST = new Set(["purpleair"]);

/* ---------- chart series helper ---------- */
// Turn flat history points into {t, v} series, dropping points that don't carry
// a numeric value for `key`. Plotting by real timestamp (not array index) lets
// sources sampled at different rates overlay correctly.
function seriesFor(points, key, color, area) {
  return {
    color,
    area: !!area,
    points: points
      .filter((p) => typeof p[key] === "number")
      .map((p) => ({ t: new Date(p.time).getTime(), v: p[key] })),
  };
}

/* ---------- theme toggle (self-initializing on every page) ---------- */
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function renderThemeToggle() {
  const theme = currentTheme();
  document.querySelectorAll(".theme-toggle button").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.getAttribute("data-theme-choice") === theme));
  });
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".theme-toggle button");
  if (!btn) return;
  const next = btn.getAttribute("data-theme-choice");
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("apollo-air1-theme", next);
  renderThemeToggle();
});

/* ---------- readout mode: AQI numbers vs engineering units ----------
 * Dashboard, Forecast, and Technical all carry this toggle and default to
 * "aqi" -- this app is for non-technical people, so every pollutant reads on
 * the one comparable 0-500 scale (and drops anything with no AQI, e.g. NH3)
 * unless someone opts into "units" for the underlying concentrations. Indoor
 * has no outside pollutants, so it doesn't show the toggle. Lives here (not
 * per-page like the temperature toggle) because every page that has it reads/
 * renders it identically; each page just re-renders its own pollutant views
 * on the readoutchange event below. */
function readoutMode() {
  return localStorage.getItem("apollo-air1-readout") || "aqi";
}
function renderReadoutToggle() {
  document.querySelectorAll(".readout-toggle button").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.getAttribute("data-readout") === readoutMode()));
  });
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".readout-toggle button");
  if (!btn) return;
  localStorage.setItem("apollo-air1-readout", btn.getAttribute("data-readout"));
  renderReadoutToggle();
  document.dispatchEvent(new CustomEvent("readoutchange"));
});

/* ---------- settings panel (self-initializing on every page) ----------
 * openSettingsPanel is exposed on window so the mode-rail (below) can pop it
 * open when Away is tapped with no location saved yet. */
(function initSettingsPanel() {
  const settingsToggle = document.getElementById("settings-toggle");
  const settingsPanel = document.getElementById("settings-panel");
  const settingsBackdrop = document.getElementById("settings-backdrop");
  if (!settingsToggle || !settingsPanel || !settingsBackdrop) return;

  function positionSettingsPanel() {
    // Below 560px the panel is a fixed bottom sheet (CSS handles placement) --
    // clear any inline position so that isn't fought.
    if (window.innerWidth <= 560) {
      settingsPanel.style.top = "";
      settingsPanel.style.right = "";
      return;
    }
    const rect = settingsToggle.getBoundingClientRect();
    const margin = 20;
    settingsPanel.style.top = `${rect.bottom + 8}px`;
    settingsPanel.style.right = `${Math.max(margin, window.innerWidth - rect.right)}px`;
  }
  function openSettings() {
    positionSettingsPanel();
    settingsPanel.hidden = false;
    settingsBackdrop.hidden = false;
    settingsToggle.setAttribute("aria-expanded", "true");
    window.addEventListener("resize", positionSettingsPanel);
  }
  function closeSettings() {
    settingsPanel.hidden = true;
    settingsBackdrop.hidden = true;
    settingsToggle.setAttribute("aria-expanded", "false");
    window.removeEventListener("resize", positionSettingsPanel);
  }
  settingsToggle.addEventListener("click", () => {
    if (settingsPanel.hidden) openSettings(); else closeSettings();
  });
  settingsBackdrop.addEventListener("click", closeSettings);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !settingsPanel.hidden) closeSettings();
  });
  window.openSettingsPanel = openSettings;
})();

/* ---------- location mode: Home / Away (self-initializing on every page) ----------
 * A third piece of shared, per-browser client state alongside theme/readout --
 * which location the whole app (dashboard, Technical, Forecast) currently
 * shows. Persisted so a wall display and a phone can independently sit in
 * different modes. The provider choice itself (apollo-air1-provider, see
 * dashboard.js) is deliberately *not* split per mode -- it used to be, but a
 * provider silently changing underneath you when you flip modes turned out
 * to be more confusing than useful. */
function currentMode() {
  return localStorage.getItem("apollo-air1-mode") || "home";
}

// undefined = not fetched yet; null = fetched, no away location saved.
let _awayLoc;

async function fetchAwayLoc(force) {
  if (_awayLoc !== undefined && !force) return _awayLoc;
  try {
    const res = await fetch("/api/away");
    _awayLoc = res.ok ? await res.json() : null;
  } catch (e) {
    _awayLoc = null;
  }
  if (_awayLoc && _awayLoc.lat == null) _awayLoc = null; // stored but unresolved
  return _awayLoc;
}

// Synchronous read of the last fetch -- callers that just need "is there one
// / what's its zip" (e.g. building a Forecast link) use this; callers driving
// the mode-rail/settings row itself await fetchAwayLoc() directly.
function getAwayLoc() {
  return _awayLoc === undefined ? null : _awayLoc;
}

function renderModeRail() {
  const mode = currentMode();
  document.querySelectorAll(".mode-rail button").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.getAttribute("data-mode") === mode));
  });
}

function setMode(mode) {
  localStorage.setItem("apollo-air1-mode", mode);
  renderModeRail();
  document.dispatchEvent(new CustomEvent("modechange", { detail: { mode } }));
}

function renderAwayLocationRow() {
  const el = document.getElementById("away-location-current");
  if (!el) return;
  const loc = getAwayLoc();
  el.textContent = loc ? (loc.reporting_area || loc.zip) : "Not set — pick a ZIP below";
}

/* ---------- provider chips: AirNow / Google / PurpleAir / OpenWeatherMap
 * (self-initializing wherever #provider-chips exists) ----------
 * Lives here (not per-page) so the persistent chip bar works identically on
 * every page that has one, rather than only the page that happened to define
 * it first. One shared choice across Home and Away (not per-mode) -- a
 * provider silently changing underneath you when you flip modes turned out
 * to be more confusing than useful. PROVIDER_NAMES/PROVIDER_ORDER above. */
function currentProvider() {
  return localStorage.getItem("apollo-air1-provider") || "airnow";
}

// Each chip shows that provider's own live AQI (from /api/outside/all, one
// best-effort call per provider server-side, no extra upstream traffic
// beyond what browsing them individually would cost) so tapping between
// sources is also how you see what the other three are reading -- not just a
// blind tab switch.
async function renderProviderChips() {
  const wrap = document.getElementById("provider-chips");
  if (!wrap) return;
  const selected = currentProvider();
  // The Forecast page marks its chip bar with this (bottom_nav.html) since
  // it's the only place a provider with no forecast (PurpleAir) can't be
  // picked at all -- everywhere else it's a perfectly valid live-conditions
  // provider.
  const restrictForecast = wrap.dataset.restrictForecast === "true";
  try {
    const res = await fetch(`/api/outside/all?mode=${currentMode()}`);
    const summary = res.ok ? await res.json() : {};
    wrap.innerHTML = PROVIDER_ORDER.map((p) => {
      const s = summary[p] || { available: false };
      const noForecast = restrictForecast && PROVIDERS_WITHOUT_FORECAST.has(p);
      const color = s.available ? bandVar(s.band) : "var(--ink-dim)";
      const aqiText = s.available && typeof s.aqi === "number" ? String(s.aqi) : "—";
      // A dim chip alone doesn't say why -- e.g. "no healthy PurpleAir sensor
      // nearby" vs. "no away location set" are both just "off" without this,
      // so the reason the API already returns goes on the chip as a hover
      // title.
      const reasonText = !s.available && s.reason ? s.reason
        : (noForecast ? `${PROVIDER_NAMES[p]} has no forecast` : "");
      const titleAttr = reasonText ? ` title="${escapeHtml(reasonText)}"` : "";
      const disabledAttr = noForecast ? " disabled" : "";
      return `<button type="button" class="provider-chip" data-provider="${p}" aria-pressed="${p === selected}" data-unavailable="${!s.available}" data-no-forecast="${noForecast}" style="--pc-color: ${color}"${titleAttr}${disabledAttr}>` +
        `<span class="pc-dot"></span>${PROVIDER_NAMES[p]} <span class="pc-aqi">${aqiText}</span></button>`;
    }).join("");
  } catch (e) {
    // Chips just stay at their last-rendered state.
  }
}

function setProvider(provider) {
  localStorage.setItem("apollo-air1-provider", provider);
  renderProviderChips();
  updateForecastLink();
  document.dispatchEvent(new CustomEvent("providerchange"));
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".provider-chip");
  if (!btn || btn.disabled) return;
  setProvider(btn.getAttribute("data-provider"));
});

// The mode rail flips the whole outside half of the app to another
// location's data -- each provider's availability/AQI in the chips needs a
// re-fetch for the new location.
document.addEventListener("modechange", renderProviderChips);

/* ---------- tab bar's Forecast link ---------- */
// One shared handler for every page's bottom tab bar: in Away mode the tab
// points at the away location's forecast, and it hides entirely for
// providers that publish no forecast (PurpleAir). Pages that can change the
// inputs (dashboard's provider chips, the mode rail) call this again on top
// of the init below.
function updateForecastLink() {
  const link = document.getElementById("forecast-link");
  if (!link) return;
  link.hidden = PROVIDERS_WITHOUT_FORECAST.has(localStorage.getItem("apollo-air1-provider") || "airnow");
  const awayLoc = currentMode() === "away" ? getAwayLoc() : null;
  link.href = awayLoc ? `/forecast?zip=${encodeURIComponent(awayLoc.zip)}` : "/forecast";
}
(function initForecastLink() {
  updateForecastLink();
  fetchAwayLoc().then(updateForecastLink);
  document.addEventListener("modechange", updateForecastLink);
})();

(function initModeRail() {
  if (!document.querySelector(".mode-rail")) return;

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".mode-rail button");
    if (!btn) return;
    const mode = btn.getAttribute("data-mode");
    if (mode === "away" && !(await fetchAwayLoc())) {
      // Nothing to flip to yet -- open the settings panel's Away location
      // row instead of switching to an empty view.
      if (window.openSettingsPanel) window.openSettingsPanel();
      return;
    }
    setMode(mode);
  });

  const form = document.getElementById("away-location-form");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = document.getElementById("away-location-zip");
      const zip = input.value;
      const btn = form.querySelector("button");
      btn.disabled = true;
      try {
        const res = await fetch("/api/away", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ zip }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(d.error || "request failed");
        _awayLoc = d;
        renderAwayLocationRow();
        input.value = "";
        // Away is now configured -- if the rail is already sitting on Away
        // (e.g. editing to change it), refresh the app for the new location.
        if (currentMode() === "away") setMode("away");
      } catch (err) {
        // The settings panel has no toast stack on every page -- a plain
        // alert-free inline message would need its own element; keeping this
        // minimal for now matches the panel's other rows (no error states).
        document.getElementById("away-location-current").textContent = "Couldn't save that ZIP";
      }
      btn.disabled = false;
    });
  }

  fetchAwayLoc().then(renderAwayLocationRow);
  renderModeRail();
})();

/* ---------- footer clock (self-initializing on every page) ---------- */
(function initClock() {
  const el = document.getElementById("footer-clock");
  if (!el) return;
  function tick() {
    el.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  tick();
  setInterval(tick, 1000);
})();

/* ---------- visibility-aware polling ----------
 * Like setInterval(fn, ms) but skips ticks while the tab is hidden (a
 * backgrounded phone/wall display shouldn't keep hammering the API), and fires
 * fn once immediately when the tab becomes visible again so stale data is
 * refreshed on return. Callers still do their own initial load at init. */
function pollInterval(fn, ms) {
  setInterval(() => { if (!document.hidden) fn(); }, ms);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) fn(); });
}

/* ---------- event-driven refresh with a timer floor ----------
 * Runs `loadFn` when the device has actually published something new, rather
 * than on a fixed schedule. Every WATCH_TICK_MS it asks /api/tick -- an in-memory
 * read on the server, no InfluxDB -- and only calls loadFn when the `seen_at`
 * token advances.
 *
 * This exists because the AIR-1 publishes within a second of its severity band
 * changing, but a page on a 60s timer cannot know that until it next asks: the
 * first reading of an event could sit a full minute before anything fetched it.
 * Polling cannot be woken by an event it has not fetched, so the fix is to make
 * the "has anything changed?" question cheap enough to ask constantly.
 *
 * `fallbackMsFor()` is a floor, not the primary mechanism. If /api/tick is
 * unreachable or the MQTT bridge is down, its token never moves and this
 * degrades to exactly the timer-based behaviour it replaced. It also bounds how
 * stale the page can get if a publish is somehow missed.
 *
 * setTimeout-chained rather than setInterval, because both the tick cadence and
 * the fallback interval are recomputed each cycle. loadFn is awaited so a slow
 * request delays the next tick instead of stacking behind it, and wrapped so a
 * single throw cannot end the chain and leave a wall display frozen forever
 * (setInterval would merely skip a tick). */
const WATCH_TICK_MS = 5000;

function watchLatest(loadFn, fallbackMsFor) {
  let timer = null;
  let lastSeen = null;
  let lastLoad = 0;

  async function run() {
    if (document.hidden) { schedule(); return; }

    let changed = false;
    try {
      const res = await fetch("/api/tick");
      if (res.ok) {
        const d = await res.json();
        // First tick establishes the baseline without forcing a load -- the
        // caller has already done its initial fetch at init.
        if (d.seen_at != null && d.seen_at !== lastSeen) {
          changed = lastSeen !== null;
          lastSeen = d.seen_at;
        }
      }
    } catch (e) { /* fall through to the timer floor */ }

    if (changed || Date.now() - lastLoad >= fallbackMsFor()) {
      lastLoad = Date.now();
      try { await loadFn(); } catch (e) { /* keep watching */ }
    }
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(run, WATCH_TICK_MS);
  }

  lastLoad = Date.now();   // the caller's own init load counts as the first one
  schedule();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); });
}

/* ---------- how fast to poll /api/latest ----------
 * The AIR-1 publishes faster during an event than at rest, so a fixed 60s poll
 * would throw most of that away: worst case a 15s-old reading takes a full
 * minute to reach the screen.
 *
 * Both the threshold and the two rates come from the device's band table
 * (`elevated_band`, `period_s`, `period_elevated_s`) rather than being repeated
 * here. Retuning the firmware's cadence therefore retunes this automatically --
 * and, more to the point, the app cannot end up polling fast at moments the
 * device isn't publishing fast, or slowly at moments it is.
 *
 * The value graded is `air_band`, the 0-5 worst-of the FIRMWARE published in the
 * snapshot -- not re-derived here from co2_ppm/aqi/voc_index.
 *
 * Getting the threshold right needed measurement on the firmware side, and the
 * firmware README has the numbers: "above green" was true ~50% of the time until
 * the VOC green band was retuned to cover the index's own resting value. This
 * side just follows whatever the device reports, whichever way that lands.
 *
 * Fallbacks are the slow rate: a missing air_band means firmware too old to have
 * an elevated rate at all, so there is nothing faster to collect, and a missing
 * table means we don't know the device's cadence and shouldn't guess at it. */
const LATEST_POLL_FALLBACK_MS = 60000;

function latestPollMs(latest) {
  const table = bandTable();
  const band = latest && latest.air_band;
  const slow = (table && table.period_s * 1000) || LATEST_POLL_FALLBACK_MS;
  const fast = (table && table.period_elevated_s * 1000) || slow;
  const threshold = table && typeof table.elevated_band === "number" ? table.elevated_band : Infinity;
  return typeof band === "number" && band >= threshold ? fast : slow;
}

/* ---------- service worker (installability is a nice-to-have) ---------- */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// Every page calls this in its own init too, but do it here so the toggle
// reflects the saved theme even before the page script runs.
renderThemeToggle();
// Reflect the saved readout choice on the pages that show the toggle.
renderReadoutToggle();
// Initial chip render (no-ops on pages with no #provider-chips) + the same
// 15-minute refresh cadence each page already polls its own outside reading
// at, so the chips' live AQI values don't go stale sitting in the fixed bar.
renderProviderChips();
pollInterval(renderProviderChips, 15 * 60000);
