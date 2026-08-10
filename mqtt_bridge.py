"""The live broker link: one subscription covering the whole HOUSE.

This bridge used to be pinned to a single device (`MQTT_TOPIC_PREFIX` =
`<house>/<device>`), which meant one container per sensor. It now subscribes to
`<house>/#` and keys everything it hears by (device, suffix), so one container
serves every room in the house -- live state and controls for all of them.

Every accessor and publish helper therefore takes an explicit `device`. That is
deliberately not optional and has no default: at QoS 0 a command published to
the wrong device topic is indistinguishable from success from this side, so
"which sensor" must be a decision the caller has already made (see
tenants.control_device), never one this module quietly makes for it.
"""

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

import tenants

logger = logging.getLogger(__name__)

# ESPHome derives each entity's MQTT object_id from its friendly `name:` (not
# its yaml `id:`), lowercased and snake_cased. These started as inferences from
# the `name:` fields in apollo-air1-mqtt-esphome's apollo-air1-mqtt.yaml, and
# were confirmed against the live device on 2026-07-24 by enumerating its
# web_server event stream (`curl -sN http://<device>/events`): every id below
# matches. The write side is safe to rely on.
#
# Re-check after any firmware change that renames an entity — a renamed `name:`
# silently changes the topic, and publishes to the old one just go nowhere.
PREVENT_SLEEP = "prevent_sleep"
# LED brightness doubles as the master: 0 % is off, so there is no separate
# enable switch. LED_ALARM_MODE picks the behaviour -- on = dark until a reading
# is dangerous then strobe, off = steady air-quality color.
# NOTE: ESPHome derives the MQTT object_id from the entity *name*, not its
# yaml id. The name is "LED Brightness", so the topic is led_brightness even
# though the id is still air_quality_led_brightness. Getting this wrong fails
# silently -- publishes land on a topic nothing subscribes to.
LED_BRIGHTNESS = "led_brightness"
# The night half of the pair. LED_BRIGHTNESS above is the DAYTIME value despite
# its name -- the firmware kept the original name when day/night dimming was
# added, because ESPHome derives both the MQTT topic and the flash preference
# key from it and renaming would have broken this topic and reset every
# deployed unit's stored brightness. The UI labels them "Day" and "Night"; only
# these two constants know about the asymmetry.
LED_NIGHT_BRIGHTNESS = "led_night_brightness"
LED_ALARM_MODE = "led_alarm_mode"
# Ambience: a decorative modulation of the steady band colour. A SELECT rather
# than two switches because ESPHome runs at most one light effect at a time, so
# the states really are mutually exclusive — a pair of toggles could represent
# a combination the device cannot be in.
LED_AMBIENCE = "led_ambience"
LED_AMBIENCE_OPTIONS = ("Off", "Breathing", "Steampunk")
LED_AMBIENCE_INTENSITY = "led_ambience_intensity"
SLEEP_DURATION = "sleep_duration"
SEN55_TEMPERATURE_OFFSET = "sen55_temperature_offset"
SEN55_HUMIDITY_OFFSET = "sen55_humidity_offset"
DPS310_PRESSURE_OFFSET = "dps310_pressure_offset"
CALIBRATE_SCD40 = "calibrate_scd40_to_420ppm"
CLEAN_SEN55 = "clean_sen55"
ESP_REBOOT = "esp_reboot"
# "Factory Reset ESP" is `disabled_by_default: true` in the firmware yaml —
# confirm it actually responds over MQTT before wiring a UI control to it.
FACTORY_RESET = "factory_reset_esp"

# Everything heard under the house, keyed by (device, suffix) -- e.g.
# ("living_room", "switch/prevent_sleep/state"). A flat {suffix: ...} dict was
# fine while one container watched one sensor; with the whole house on one
# subscription it would have two rooms overwriting each other's `state` and
# `status`, silently reporting whichever published last as both.
_state = {}
_lock = threading.Lock()
_client = None


def _topic_root():
    """The MQTT topic prefix this container is wired to: everything ABOVE the
    device segment, no trailing slash.

    Since plan 05 this is NOT one segment, and NOT the same string as the
    account. The estate convention is

        cosmoslab/<category>/<account>/<vendor>/<model>/<device>/<channel...>

    so the prefix is the first five segments while the account is only the
    third of them. `tenants.own()` still returns the ACCOUNT (`walnut`) because
    that is the InfluxDB `tenant` tag and the thing ownership checks are about.
    Conflating "which account are we" with "where do this device's topics live"
    is precisely what this split prevents: they were the same string for as long
    as the account was segment 0, and they are not any more.

    Falls back to `tenants.own()` so a deployment still on the pre-plan-05
    single-segment tree keeps working with no env change.

    Scoped to one vendor+model (`.../apollo/air1`), which is honest -- this is
    an AIR-1 dashboard. If this house ever runs a second *model*, `_on_message`
    and `_command_topic` need a device -> (vendor, model) map rather than one
    shared prefix."""
    root = os.environ.get("MQTT_TOPIC_ROOT", "").strip("/")
    return root or tenants.own()


def _on_connect(client, userdata, flags, reason_code, properties):
    house = _topic_root()
    if not house:
        # Subscribing to "#" here would pull in every tenant on the broker (and
        # be denied by the ACL anyway). Loud, and no subscription.
        logger.error("mqtt_bridge: no MQTT_TOPIC_ROOT/MQTT_HOUSE_PREFIX set; not subscribing")
        return
    client.subscribe(f"{house}/#")
    logger.info("mqtt_bridge: connected to broker, subscribed to %s/#", house)


def _on_disconnect(client, userdata, flags, reason_code, properties):
    logger.warning("mqtt_bridge: disconnected from broker (reason=%s)", reason_code)


def _on_message(client, userdata, msg):
    house = _topic_root()
    if not house:
        return
    prefix = house + "/"
    if not msg.topic.startswith(prefix):
        return
    # `<topic-root>/<device>/<suffix...>`. Because the root already covers
    # everything above the device, the segment straight after it IS the device
    # -- the same position Node-RED reads (parts[5] on the six-segment tree).
    # partition rather than split(maxsplit) because the suffix keeps its own
    # slashes: "switch/prevent_sleep/state" is one key, not three.
    device, _, suffix = msg.topic[len(prefix):].partition("/")
    if not device or not suffix:
        # A house-level topic with nothing under it. Nothing reads those, and
        # storing them under a device that doesn't exist would put junk in the
        # picker's way if it ever learned devices from here.
        return
    payload = msg.payload.decode("utf-8", errors="replace")
    with _lock:
        _state[(device, suffix)] = {"value": payload, "seen_at": time.time()}
    logger.debug("mqtt_bridge: %s/%s = %r", device, suffix, payload)


def start():
    """Start the background MQTT client. Safe to call once at app boot.

    Never raises: this dashboard is mostly read-only Influx views, and those
    must stay up even if the broker is unreachable. connect_async + loop_start
    means the initial connect happens on the network thread (with automatic
    retry via reconnect_delay_set), so a down broker at boot degrades to
    "controls unavailable" instead of crashing the whole app."""
    global _client
    if _client is not None:
        return

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        username = os.environ.get("MQTT_USERNAME")
        if username:
            client.username_pw_set(username, os.environ.get("MQTT_PASSWORD"))
        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        broker = os.environ["MQTT_BROKER"]
        port = int(os.environ.get("MQTT_PORT", 1883))
        client.connect_async(broker, port, keepalive=60)
        client.loop_start()
        _client = client
        logger.info("mqtt_bridge: connecting to %s:%s (async)", broker, port)
    except Exception:
        logger.exception("mqtt_bridge: failed to start MQTT client; controls disabled")


def available():
    """True when we actually have a live broker connection to publish onto.

    The control endpoints check this to return a clean 503 (rather than an
    AttributeError, or a hollow "published: True" into a dropped message) when
    the broker is unreachable. Note this reflects the app<->broker link, which
    stays up even while the AIR-1 itself deep-sleeps -- so a command to a
    sleeping device still returns 200 best-effort, exactly as before; only a
    genuinely down broker trips the 503."""
    if _client is None:
        return False
    try:
        return _client.is_connected()
    except Exception:
        return False


def device_online(device):
    """True when that device is on the broker, per its birth/LWT status.

    Distinct from available() above, which only says the *app* can reach the
    broker. Controls need both: a command is published QoS 0 and non-retained
    to a topic only the device subscribes to, so one sent while it is
    disconnected is dropped there and then -- the broker does not hold it for
    the device's return, and nothing ever replays it.

    Per device, because in a multi-room house one sensor being awake says
    nothing about another: they deep-sleep on independent cycles."""
    with _lock:
        return _state.get((device, "status"), {}).get("value") == "online"


def get_raw(device, suffix):
    """Raw payload last seen on one device's topic suffix, or None.

    _on_message already caches every topic under `<house>/#`, so reading a
    topic a *device* publishes -- rather than one of the control entities
    get_state() knows about -- needs no new subscription, just this accessor.
    Used by bands.py for the retained `config/bands` table.
    """
    with _lock:
        entry = _state.get((device, suffix))
    return entry["value"] if entry else None


def get_seen(device, suffix):
    """When we last received anything on one device's topic suffix, or None.

    Deliberately the RECEIVE time rather than any timestamp inside the payload:
    /api/tick uses it purely as a change token, and the device's own snapshots
    carry no clock of their own. Monotonic per message, which is all a
    change-detector needs.
    """
    with _lock:
        entry = _state.get((device, suffix))
    return entry["seen_at"] if entry else None


def _val(snapshot, suffix):
    entry = snapshot.get(suffix)
    return entry["value"] if entry else None


def _seen(snapshot, suffix):
    entry = snapshot.get(suffix)
    return entry["seen_at"] if entry else None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _led_ramp_readback(snapshot):
    """The day/night ramp's current position, parsed out of the device's
    combined `/state` snapshot.

    Deliberately reads the snapshot rather than adding two more MQTT entities
    to the device: the firmware folds both values into the message it already
    publishes every cycle, so this costs no extra broker traffic. Same accessor
    path bands.py uses for the retained band table.

    Every failure mode collapses to None -- no snapshot yet, firmware older
    than the release that added the fields, or a malformed payload. The UI
    treats None as "the device hasn't told us", which is honest: this is the
    only part of the LED card the app cannot infer from the two setpoints."""
    raw = _val(snapshot, "state")
    if not raw:
        return {"led_brightness_effective": None, "sun_elevation_deg": None}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {"led_brightness_effective": None, "sun_elevation_deg": None}
    if not isinstance(payload, dict):
        return {"led_brightness_effective": None, "sun_elevation_deg": None}

    effective = payload.get("led_brightness_effective_pct")
    # -1 is the firmware's "not computed yet" sentinel (the first seconds after
    # boot), not a brightness. Surface it as unknown rather than as a number.
    if not isinstance(effective, (int, float)) or isinstance(effective, bool) or effective < 0:
        effective = None
    elevation = payload.get("sun_elevation_deg")
    if not isinstance(elevation, (int, float)) or isinstance(elevation, bool):
        elevation = None
    return {"led_brightness_effective": effective, "sun_elevation_deg": elevation}


def _snapshot(device):
    """One device's slice of the cache, re-keyed by bare suffix.

    Everything below (_val, _seen, _led_ramp_readback) parses a single
    device's view of the world, so the device dimension is peeled off once
    here rather than threaded through every lookup -- which also keeps those
    parsers unit-testable against a plain {suffix: entry} dict."""
    with _lock:
        return {suffix: entry for (dev, suffix), entry in _state.items() if dev == device}


def get_state(device):
    snapshot = _snapshot(device)

    return {
        "online": _val(snapshot, "status") == "online",
        "status_seen_at": _seen(snapshot, "status"),
        "prevent_sleep": _val(snapshot, f"switch/{PREVENT_SLEEP}/state") == "ON",
        "led_brightness": _to_float(_val(snapshot, f"number/{LED_BRIGHTNESS}/state")),
        "led_night_brightness": _to_float(_val(snapshot, f"number/{LED_NIGHT_BRIGHTNESS}/state")),
        "led_alarm_mode": _val(snapshot, f"switch/{LED_ALARM_MODE}/state") == "ON",
        # Passed through as the device's own string rather than mapped to a
        # bool/enum here: it has to survive a round trip back to the device's
        # select, whose options are matched by exact name.
        "led_ambience": _val(snapshot, f"select/{LED_AMBIENCE}/state"),
        "led_ambience_intensity": _to_float(_val(snapshot, f"number/{LED_AMBIENCE_INTENSITY}/state")),
        # Where the day/night ramp actually is right now, read out of the
        # device's combined /state snapshot rather than from a control topic:
        # these are reported values, not settings, and they ride the snapshot
        # the device already publishes. None on older firmware, and
        # sun_elevation_deg is also None until the device's clock has synced
        # (the firmware omits it rather than sending a 0 that would read as
        # dusk), which is exactly when the ramp is holding daytime brightness.
        **_led_ramp_readback(snapshot),
        "sleep_duration_min": _to_float(_val(snapshot, f"number/{SLEEP_DURATION}/state")),
        "sen55_temperature_offset": _to_float(_val(snapshot, f"number/{SEN55_TEMPERATURE_OFFSET}/state")),
        "sen55_humidity_offset": _to_float(_val(snapshot, f"number/{SEN55_HUMIDITY_OFFSET}/state")),
        "dps310_pressure_offset": _to_float(_val(snapshot, f"number/{DPS310_PRESSURE_OFFSET}/state")),
        "seen_at": {
            "prevent_sleep": _seen(snapshot, f"switch/{PREVENT_SLEEP}/state"),
            "led_brightness": _seen(snapshot, f"number/{LED_BRIGHTNESS}/state"),
            "led_night_brightness": _seen(snapshot, f"number/{LED_NIGHT_BRIGHTNESS}/state"),
            "led_alarm_mode": _seen(snapshot, f"switch/{LED_ALARM_MODE}/state"),
            "led_ambience": _seen(snapshot, f"select/{LED_AMBIENCE}/state"),
            "led_ambience_intensity": _seen(snapshot, f"number/{LED_AMBIENCE_INTENSITY}/state"),
            "sleep_duration_min": _seen(snapshot, f"number/{SLEEP_DURATION}/state"),
            "sen55_temperature_offset": _seen(snapshot, f"number/{SEN55_TEMPERATURE_OFFSET}/state"),
            "sen55_humidity_offset": _seen(snapshot, f"number/{SEN55_HUMIDITY_OFFSET}/state"),
            "dps310_pressure_offset": _seen(snapshot, f"number/{DPS310_PRESSURE_OFFSET}/state"),
        },
    }


def _command_topic(device, kind, object_id):
    """`<topic-root>/<device>/<kind>/<object_id>/command`.

    One builder for all four publish helpers so the device segment cannot be
    left out of one of them -- the resulting topic would still look plausible
    (`walnut/switch/...`), be denied by the ACL at QoS 0, and report success."""
    return f"{_topic_root()}/{device}/{kind}/{object_id}/command"


def publish_switch(device, object_id, on):
    _client.publish(_command_topic(device, "switch", object_id), "ON" if on else "OFF")


def publish_number(device, object_id, value):
    _client.publish(_command_topic(device, "number", object_id), str(value))


def publish_select(device, object_id, option):
    """Set a select entity. ESPHome matches the option by exact string, so the
    value published here has to be one the device's list actually contains — an
    unknown option is logged on the device and otherwise ignored, which from
    here is indistinguishable from success. app.py validates against
    LED_AMBIENCE_OPTIONS before calling."""
    _client.publish(_command_topic(device, "select", object_id), option)


def publish_button(device, object_id):
    _client.publish(_command_topic(device, "button", object_id), "PRESS")


def publish_config(topic, payload, retain=True):
    """Publish a JSON config document to `topic`, retained by default so a
    subscriber (Node-RED's home-config mqtt-in) picks up the current value the
    moment it connects, even after a restart. Best-effort like the control
    publishes: returns False when the broker link isn't up rather than raising,
    so setting the home location degrades to "saved but not yet pushed" instead
    of 500ing."""
    if not available():
        return False
    _client.publish(topic, json.dumps(payload), qos=1, retain=retain)
    return True
