# Apollo AIR-1 Dashboard — Cosmos Lab

Flask app for the Apollo AIR-1 air quality sensor: current readings + history
from InfluxDB, live device controls over MQTT, and an outdoor AQI comparison
via AirNow. No Home Assistant, no Grafana — self-contained. Two views: a
plain-language **Simple** view for anyone in the house, and a **Technical**
view with full instrument readouts, calibration controls, and history charts
— reaching parity with (and extending) the device's own onboard ESPHome web
UI.

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
  online/offline (from its MQTT birth/LWT `status` topic).
- `POST /api/control/switch/<id>`, `/api/control/number/<id>`,
  `/api/control/button/<id>` — publish a command. The AIR-1 deep-sleeps
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

Polling still pauses entirely while the tab is hidden, and fires once
immediately when it becomes visible again (`pollAdaptive` in `common.js`).

## Security boundary

There is **no authentication** on this app, including the mutating
`/api/control/*` routes (reboot, factory-reset, calibration). That's
deliberate for a LAN-only deployment: the app and the AIR-1 live on the same
trusted home network, and access is expected to be gated at the network edge
(LAN-only / Cloudflare Access), not in-app. If this is ever exposed more
broadly, add a shared-secret/token check on the `/api/control/*` routes (the
three handlers already share a single guard helper, so it's a one-place
change) rather than relying on the network boundary alone.

## Related repos

- [`apollo-air1-mqtt-esphome`](../apollo-air1-mqtt-esphome) — the ESPHome
  firmware and MQTT payload schema.
- [`coslab-nodered-flows`](../coslab-nodered-flows) — the flow that writes
  into `air_quality`.
