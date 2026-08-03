(function () {
  "use strict";

  // fmt / timeAgo / escapeHtml / pollInterval come from common.js (loaded
  // first). Theme toggle, settings panel, and clock self-init there too.
  //
  // This page is the AIR-1 itself: diagnostics, the LED key, and every
  // device control (calibration, sleep, factory reset, home location) --
  // split out of indoor.js when those sections moved off the Inside page.
  // No aqi.js/chart.js: nothing here is graded or charted.

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
    loadDiagnostics();
    loadControls();
  });

  /* ---------- toast ---------- */
  function toast(msg) {
    const stack = document.getElementById("toast-stack");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }

  /* ---------- diagnostics (from the same snapshot the readouts use) ---------- */
  const DIAG_FIELD_IDS = ["d-rssi", "d-esptemp", "d-uptime", "d-firmware", "d-since"];

  async function loadDiagnostics() {
    try {
      const res = await fetch("/api/latest");
      if (!res.ok) throw new Error("request failed");
      const d = await res.json();
      document.getElementById("d-rssi").textContent = fmt(d.wifi_rssi_db, 0) + " dB";
      document.getElementById("d-esptemp").textContent = fmt(displayTemp(d.esp_temperature_c), 1) + " " + tempUnitLabel();
      const uptimeMin = typeof d.uptime_s === "number" ? d.uptime_s / 60 : null;
      document.getElementById("d-uptime").textContent = fmt(uptimeMin, 1) + " min";
      document.getElementById("d-firmware").textContent = d.firmware_version || "—";
      document.getElementById("d-since").textContent = timeAgo(d.time);
    } catch (e) {
      DIAG_FIELD_IDS.forEach((id) => { document.getElementById(id).textContent = "—"; });
    }
  }

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

  // Rockers ship indeterminate (aria-pressed="mixed", disabled) and only become
  // real controls once the device's state has actually been read. Rendering a
  // confident "off" before then is a claim the page is in no position to make,
  // and it reads identically to a device whose alarm really is off.
  function setRocker(id, on) {
    const el = document.getElementById(id);
    el.setAttribute("aria-pressed", String(on));
    el.disabled = false;
  }

  function showControlsProblem(detail) {
    const note = document.getElementById("controls-status");
    note.textContent = "Can't read the device's settings — " + detail;
    note.hidden = false;
  }

  async function loadControls() {
    try {
      const res = await fetch("/api/controls", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`the dashboard answered ${res.status}`);
      // An expired Cloudflare Access session answers with a 200 HTML login
      // page, not a 401 -- res.ok is true and res.json() then dies on "<".
      // Without this check that lands in the catch below as an unexplained
      // parse error, and every control silently keeps its pre-load default.
      const type = res.headers.get("content-type") || "";
      if (!type.includes("application/json")) {
        throw new Error("something answered instead of the app; try signing in again");
      }
      const s = await res.json();

      setRocker("rocker-sleep", !!s.prevent_sleep);

      // Don't fight the user mid-drag: only adopt the device's value when the
      // slider isn't focused, or a poll landing between drag and publish would
      // snap the handle back.
      const bright = document.getElementById("led-brightness");
      if (typeof s.led_brightness === "number" && document.activeElement !== bright) {
        bright.value = String(s.led_brightness);
        document.getElementById("val-led-brightness").textContent = String(Math.round(s.led_brightness));
      }
      setRocker("rocker-led-mode", !!s.led_alarm_mode);
      syncLedBrightnessHelp(!!s.led_alarm_mode);

      Object.entries(stepperConf).forEach(([key, conf]) => {
        const v = s[conf.stateKey];
        if (typeof v === "number") {
          stepperState[key] = v;
          document.getElementById("val-" + key).textContent = displayStepperValue(conf, v);
        }
      });
      document.getElementById("controls-status").hidden = true;
    } catch (e) {
      // Values already read stay put -- a dropped poll is no reason to blank a
      // reading that was true a moment ago. What must not happen is failing
      // silently: before this, an unreachable API left every control sitting at
      // its markup default, which for the rockers meant a definite-looking
      // "off" that outlived refreshes and looked exactly like a lost write.
      console.error("controls read failed:", e);
      // fetch() rejects with a bare TypeError for anything that never reached a
      // server, whose message ("Failed to fetch") means nothing on a phone.
      showControlsProblem(e instanceof TypeError ? "no connection to the dashboard" : e.message);
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
        // Put the step back. The optimistic bump is what makes the control feel
        // instant, but leaving it up after a refused write shows a value the
        // device never got -- the same lie the rockers roll back from.
        stepperState[key] = base;
        document.getElementById("val-" + key).textContent = displayStepperValue(conf, base);
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

  // PurpleAir sensor status isn't repeated here -- it's already reflected in
  // the source picker (the PurpleAir row goes unavailable, with the real
  // reason on it, when there's no healthy sensor nearby).
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

  /* ---------- init ---------- */
  renderUnitToggle();
  loadDiagnostics();
  loadControls();
  loadHome();
  pollInterval(loadDiagnostics, 60000);
  pollInterval(loadControls, 30000);
})();
