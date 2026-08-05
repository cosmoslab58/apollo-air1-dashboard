# Apollo AIR-1 Dashboard — Cosmos Lab

Flask app for the Apollo AIR-1 air quality sensor: current readings + history
from InfluxDB, live device controls over MQTT, and an outdoor AQI comparison
via AirNow. No Home Assistant, no Grafana — self-contained. Five pages:
**Overview** (a plain-language Inside/Outside pair, each card tappable
through to its details), **Inside** and **Outside** (full instrument
readouts and history charts), **Forecast**, and a **Device** page
(diagnostics, LED key, calibration and other setup controls — reaching
parity with, and extending, the device's own onboard ESPHome web UI) reached
from the settings gear. The outdoor data source (AirNow/Google/PurpleAir/OWM)
is picked from the "via X" stamp next to any outside reading.

```
Apollo AIR-1  <-->  mosquitto  -->  Node-RED  -->  InfluxDB (air_quality)  -->  this app
                        ^ commands (switch/number/button)  |
                        +-----------------------------------+
AirNow API  -->  this app (outdoor AQI, cached hourly)
```

## Stack

Flask + a hand-rolled SVG line chart (no JS framework, no CDN dependency) for
current-value tiles and CO2 / particulate / VOC-NOx / temperature / humidity
history, a plain table for the MICS-4514 gas sensor readings and device
diagnostics, and a background `paho-mqtt` client (`mqtt_bridge.py`) that
mirrors the device's switches/numbers/buttons and publishes commands to them.

## Running

```
cp .env.example .env    # fill in INFLUX_TOKEN, MQTT_*, AIRNOW_* — see below
mkdir -p data && chmod 777 data   # bind-mounted; container runs as a non-root user
docker compose up -d --build
```

Then open `http://<host>:5960`.

> **Deploying alongside a firmware change: flash the AIR-1 first.**
> This app takes its severity thresholds from a **retained** MQTT message the
> device publishes when it connects (see
> [Severity bands](#severity-bands--the-device-owns-them)). Deploy the app before
> the firmware and `/api/bands` has nothing to serve, so every page renders
> uncoloured until the device next connects — which on a deep-sleep duty cycle
> can be minutes. OTA the firmware first and the retained table is already
> waiting on the broker when the container starts.
>
> After the first successful start the table is cached to `data/bands.json`, so
> this only bites on a genuinely first deploy or after the data volume is wiped.

For local dev without Docker:
```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Credentials

- `INFLUX_TOKEN` — **read-only** token scoped to the `air_quality` bucket,
  not the admin token used by the `iot` stack. This app only ever reads from
  Influx — every provider's current reading and history (AirNow, Google,
  PurpleAir, OpenWeatherMap) is written by Node-RED (see the Apollo AIR-1
  flow), not by this app. Rotate in the InfluxDB UI
  (`http://192.168.4.113:8086`, org `cosmoslab`) under *Load Data → API
  Tokens* if it ever leaks.
- `MQTT_USERNAME` / `MQTT_PASSWORD` — mosquitto credentials for this app's
  own client (publishes commands, subscribes to state topics under
  `MQTT_TOPIC_PREFIX`). `MQTT_TOPIC_PREFIX` must match the `mqtt_topic`
  substitution in [`apollo-air1-mqtt-esphome`](../apollo-air1-mqtt-esphome)'s
  `apollo-air1-mqtt.yaml`.
- `AIRNOW_API_KEY` / `AIRNOW_ZIP` — from [docs.airnowapi.org](https://docs.airnowapi.org/),
  used for the Technical view's outdoor AQI card. Responses are cached ~55min
  in memory (AirNow itself updates hourly).

## API

- `GET /api/tick` — `{seen_at, air_band}` read from the MQTT bridge's in-memory
  cache. No InfluxDB, no network. Polled every 5s by the pages purely as a
  change token: when `seen_at` advances they fetch `/api/latest`, so a new
  reading reaches the screen within ~5s instead of waiting out a poll interval.
  Nulls mean "no information" (broker down, nothing received yet), not an error.
- `GET /api/latest` — most recent reading as flat JSON. Includes `air_band`,
  the 0–5 worst-of severity band the *firmware* graded that reading at (see
  [Refresh cadence](#refresh-cadence)). `null` on points written before the
  firmware that added the field.
- `GET /api/history?hours=24` — time series for all fields over the given
  window (1–168h), used by the charts.
- `GET /api/outside` — current outdoor AQI/category/dominant pollutant from
  AirNow for `AIRNOW_ZIP`.
- `GET /api/forecast?zip=<zip>` — AirNow's forecast for a zip (defaults to
  `AIRNOW_ZIP`). AirNow only issues forecasts for today and, where available,
  tomorrow — the response has however many days it actually published, never
  padded out to a full week.
- `GET /api/locations`, `POST /api/locations` (`{label, zip}`),
  `DELETE /api/locations/<zip>` — saved locations for the forecast switcher,
  persisted to `data/locations.json` (bind-mounted, see above, so they
  survive `docker compose up --build`).
- `GET /api/controls` — cached state of the device's switches/numbers plus
  online/offline (from its MQTT birth/LWT `status` topic). Also carries
  `led_brightness_effective` and `sun_elevation_deg`, read out of the device's
  combined `/state` snapshot rather than from a control topic — see
  [Day/night LED](#daynight-led). Both are `null` on firmware older than the
  release that added them.
- `POST /api/control/switch/<id>`, `/api/control/number/<id>`,
  `/api/control/select/<id>` (`{option}`, validated against the device's own
  option list), `/api/control/button/<id>` — publish a command. The AIR-1 deep-sleeps
  between reads, so commands are **best-effort**: they're sent immediately
  but only take effect once the device is next awake and connected. Return
  `503` if the MQTT broker isn't currently reachable.
- `GET /healthz` — liveness probe (used by the container healthcheck). Always
  `200` while the process is up; deliberately does **not** depend on InfluxDB
  or MQTT, so an upstream outage doesn't make the container look dead. Reports
  `mqtt_connected` as a hint.

## Severity bands — the device owns them

**This app defines no thresholds.** Every band colour on every page comes from a
table the AIR-1 publishes to a retained MQTT topic, which `bands.py` re-serves
at `/api/bands` and `common.js` grades against.

It used to keep its own copy, and the copies had drifted:

| Channel | This app used to say | Firmware says |
|---|---|---|
| CO2 | 1000 / 1500 / 2000 | 800 / 1100 / 2000 / 3500 / 5000 |
| VOC | 150 / 250, no green band | 150 / 250 / 400 |
| NOx | never coloured at all | 20 / 150 / 250 / 400 |
| AQI | 50 / 100 / 150 | 50 / 100 / 150 / 200 / 300 |

So 900 ppm rendered green here while the LED on the device was yellow, and the
Overview headline could only ever be "driven by" PM2.5 or CO2 — never VOC, which
is in fact this unit's worst channel 94% of the time it is above green.

The firmware won the tie-break because the device has to colour its LED with no
network at all. It must hold the thresholds regardless, so a copy here would
necessarily be the *second* copy, not the only one. The numbers are also health
judgements with citations (CDC/ASHRAE/OSHA for CO2, EPA breakpoints for AQI),
and they belong next to that reasoning.

Two consequences worth knowing:

- **No table, no colour.** If the device has never published one, `/api/bands`
  returns 404 and readings render uncoloured rather than in a colour this app
  invented — inventing one is how the two drifted apart. The last known table is
  cached to `data/bands.json`, so a broker outage doesn't strip the colour out.
- **Outdoor AQI now uses the device's AQI cuts too.** Those cuts *are* the EPA
  categories, so this is a strict improvement (previously everything above 150
  was one flat "bad"; there are now bands at 200 and 300). But it does mean the
  outside cards depend on the AIR-1 having published at some point.

`aqi.js` still converts concentrations to AQI numbers locally — that's EPA's
published breakpoint standard, safe to implement anywhere. What a given AQI
number *means* on the severity scale is the judgement, and that comes from the
device.

## Day/night LED

The Device page's Status LED card has two brightness sliders, **Day** and
**Night**. The device eases the steady band colour between them across twilight
— fully day at sun elevation +3°, fully night at −6°, linear in between, so the
fade is roughly 35–50 min here and lengthens in winter on its own.

**The device does the fade, not this app and not Node-RED.** The obvious
alternative — Node-RED computing a curve and publishing to the brightness
command topic every minute — was rejected because that topic *is* where the
user's setting is stored: overwriting it every minute leaves nowhere to hold
"what I want at night", and the slider would read 12% at midnight and 100% at
noon. It would also stop working whenever Node-RED did, and write flash on
every step. The firmware got `time:` (SNTP) and `sun:` instead.

Two consequences for this app:

- **Both sliders are plain setpoints.** The interesting value — where the ramp
  actually is right now — is *reported* by the device in its `/state` snapshot
  (`led_brightness_effective_pct`, `sun_elevation_deg`) and shown under the
  sliders. This app deliberately does no sun arithmetic of its own: it has no
  clock agreement with the AIR-1 and no copy of the fade curve, so anything it
  computed could contradict the light in the room. Same argument as the band
  table above.
- **Night at 0 is the "dark until an emergency" setup.** It reuses the existing
  master-off (brightness 0 ⇒ LED off), so there's no extra switch. The danger
  strobe runs at 100% and ignores both sliders, as it always has.

The device takes its coordinates from the same retained `config/home` message
this app already publishes for Node-RED (see `home_config.py`), falling back to
a compile-time default. Changing home in the app re-points the sun calculation
too, with no firmware rebuild.

**Night can never exceed day.** The firmware enforces it (the device's own web
UI and a raw MQTT publish can set these too), and this app enforces it live so
you never see the device correct you: dragging Night up hard-stops at the Day
value, and dragging Day down carries Night with it, visibly. Both sliders keep
the same 0–100 scale — capping the Night input's `max` would put two adjacent
sliders on different scales, which reads as a rendering bug rather than as a
constraint.

## Ambience effects

The Status LED card can also run one of two decorative brightness effects,
**Breathing** or **Steampunk**, with an intensity slider. They vary the
brightness around whatever you've set; they never change the colour, the band,
or how often the device publishes.

Three things about the controls:

- **A segmented picker, not toggles.** The device runs at most one light effect
  at a time, so the states really are mutually exclusive and a set of switches
  could display a combination the device can never be in.
- **This needed select support**, which the app had never had — `publish_select`,
  a `SELECT_OPTIONS` allowlist and `POST /api/control/select/<id>`. Options are
  validated against the device's real list here, because the device matches them
  by exact string and silently ignores anything else: an unvalidated passthrough
  would turn a typo into a control that reports success and does nothing.
- **Intensity 0 is a flat effect, not off.** The picker is the off switch.

The effects stand down on their own above band 1 (orange and up), during the
danger strobe, and while the boot self-test owns the light — decoration never
softens a warning. See the firmware README for the waveforms and for why they
are ESPHome effects rather than something driven from here.

**Brightness sets the ceiling on how smooth they can look, and the page does not
currently say so.** A comfortable indoor brightness is a very small PWM number
— 33 % is 11/255 — so there are only a handful of hardware levels for the effect
to move through. The firmware dithers to synthesise more, which is what makes
the defaults usable, but it cannot invent levels that aren't there: below about
20 % both effects visibly step, and at 12 % the LED is at 0.7/255 and there is
nothing left to modulate. Turning **intensity up** also helps, counter-
intuitively — a wider swing crosses more levels on the way. If an effect looks
choppy, raise brightness or intensity rather than assuming the waveform is
wrong.

## Refresh cadence

The Overview and Indoor pages poll `/api/latest` at a rate that follows the
device, not a fixed timer:

| Situation | Poll |
|---|---|
| `air_band` is 0 (green), missing, or the feed is down | every 60s |
| `air_band` is 1 or above | every 15s |

Both the threshold and the two rates are read from the device's band table
(`elevated_band`, `period_s`, `period_elevated_s`), not hardcoded here — so
retuning the firmware's cadence retunes this automatically, and the app cannot
end up polling fast at moments the device isn't publishing fast. The firmware
also pushes a point within a second of *any* band change. Without the matching
change here, a fixed 60s poll would have thrown most of that away: a reading
published 15s after an event started could still take a full minute to appear on
screen.

Two deliberate choices:

- **The band comes from the device, not from this app.** `latestPollMs` reads
  the published `air_band` rather than recomputing it from `co2_ppm`/`aqi`/
  `voc_index`. This app's *display* bands (`bandFromCo2` and friends in
  `common.js`) use different cutoffs from the firmware's LED bands, so deriving
  it locally would mean polling fast at moments the device wasn't publishing
  fast, and vice versa.
- **Only the current-reading tiles speed up.** History charts stay on a flat
  60s — a much heavier Flux query, and an hours-long chart gains far less from
  a 15s refresh than the live readouts do. Outside providers stay at 15 min;
  they are hourly-ish feeds upstream, so polling harder just spends API quota
  to receive the same numbers.

**The interval above is a floor, not the trigger.** The device publishes within a
second of its band changing, but a page on a 60s timer cannot know that until it
next asks — polling can't be woken by an event it hasn't fetched, so the first
reading of an event could sit up to a minute before anything picked it up.

`watchLatest` (`common.js`) closes that by polling `/api/tick` every 5s and
fetching `/api/latest` only when the token moves. A band change therefore shows
up in ~5s rather than up to 60s. If `/api/tick` or the MQTT bridge is
unavailable the token never moves and this degrades to exactly the timer-based
behaviour, which is also what bounds staleness if a publish is ever missed.

Server-Sent Events would be the textbook answer and are the wrong one here:
gunicorn runs with one worker and eight threads (one worker because the MQTT
bridge holds in-process state), so every open SSE connection would pin a thread
for the life of the tab. A wall display, a phone and a laptop would be three, and
running out blocks the whole app rather than merely slowing a refresh. Frequent
cheap polls degrade better on this deployment.

Polling still pauses entirely while the tab is hidden, and fires once
immediately when it becomes visible again.

## Security boundary

There is **no authentication** on this app, including the mutating
`/api/control/*` routes (reboot, factory-reset, calibration). That's
deliberate for a LAN-only deployment: the app and the AIR-1 live on the same
trusted home network, and access is expected to be gated at the network edge
(LAN-only / Cloudflare Access), not in-app. If this is ever exposed more
broadly, add a shared-secret/token check on the `/api/control/*` routes (the
three handlers already share a single guard helper, so it's a one-place
change) rather than relying on the network boundary alone.

The **broker** is a separate boundary and is no longer wide open. As of
2026-08-05 this app authenticates with its own account (`nathan`), scoped by
topic ACL to `cosmos-lab/smarthome/air1/#` and nothing else — it cannot see or
write another device's tree, let alone another tenant's. Note the consequence
when changing `MQTT_TOPIC_PREFIX`: a prefix outside that root is **silently
denied** at QoS 0, so the app will look healthy and simply stop receiving.
Config and rationale live in
[`coslab-mqtt-broker`](https://github.com/cosmoslab58/coslab-mqtt-broker).

## Hosting a second instance

Broker-side tenant isolation is done, but this app is **not yet multi-tenant**.
It reads one `MQTT_TOPIC_PREFIX` per container, so a second tenant means a
second container today. They would also need their own API keys for all four
providers, and the Node-RED ingestion flow still hardcodes `cosmos-lab/...`.
See the Phase 5 notes in `coslab-mqtt-broker`.

## Related repos

- [`apollo-air1-mqtt-esphome`](../apollo-air1-mqtt-esphome) — the ESPHome
  firmware and MQTT payload schema.
- [`coslab-nodered-flows`](../coslab-nodered-flows) — the flow that writes
  into `air_quality`.
- [`coslab-mqtt-broker`](../coslab-mqtt-broker) — broker accounts, topic ACLs,
  and the watcher that alerts when a publish is denied.
