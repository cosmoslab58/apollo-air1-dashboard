import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

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
LED_ALARM_MODE = "led_alarm_mode"
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

_state = {}
_lock = threading.Lock()
_client = None


def _topic_prefix():
    return os.environ["MQTT_TOPIC_PREFIX"]


def _on_connect(client, userdata, flags, reason_code, properties):
    prefix = _topic_prefix()
    client.subscribe(f"{prefix}/#")
    logger.info("mqtt_bridge: connected to broker, subscribed to %s/#", prefix)


def _on_disconnect(client, userdata, flags, reason_code, properties):
    logger.warning("mqtt_bridge: disconnected from broker (reason=%s)", reason_code)


def _on_message(client, userdata, msg):
    prefix = _topic_prefix() + "/"
    if not msg.topic.startswith(prefix):
        return
    suffix = msg.topic[len(prefix):]
    payload = msg.payload.decode("utf-8", errors="replace")
    with _lock:
        _state[suffix] = {"value": payload, "seen_at": time.time()}
    logger.debug("mqtt_bridge: %s = %r", suffix, payload)


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


def device_online():
    """True when the AIR-1 itself is on the broker, per its birth/LWT status.

    Distinct from available() above, which only says the *app* can reach the
    broker. Controls need both: a command is published QoS 0 and non-retained
    to a topic only the device subscribes to, so one sent while it is
    disconnected is dropped there and then -- the broker does not hold it for
    the device's return, and nothing ever replays it."""
    with _lock:
        return _state.get("status", {}).get("value") == "online"


def get_raw(suffix):
    """Raw payload last seen on one topic suffix under the prefix, or None.

    _on_message already caches every topic under `<prefix>/#`, so reading a
    topic the *device* publishes -- rather than one of the control entities
    get_state() knows about -- needs no new subscription, just this accessor.
    Used by bands.py for the retained `config/bands` table.
    """
    with _lock:
        entry = _state.get(suffix)
    return entry["value"] if entry else None


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


def get_state():
    with _lock:
        snapshot = dict(_state)

    return {
        "online": _val(snapshot, "status") == "online",
        "status_seen_at": _seen(snapshot, "status"),
        "prevent_sleep": _val(snapshot, f"switch/{PREVENT_SLEEP}/state") == "ON",
        "led_brightness": _to_float(_val(snapshot, f"number/{LED_BRIGHTNESS}/state")),
        "led_alarm_mode": _val(snapshot, f"switch/{LED_ALARM_MODE}/state") == "ON",
        "sleep_duration_min": _to_float(_val(snapshot, f"number/{SLEEP_DURATION}/state")),
        "sen55_temperature_offset": _to_float(_val(snapshot, f"number/{SEN55_TEMPERATURE_OFFSET}/state")),
        "sen55_humidity_offset": _to_float(_val(snapshot, f"number/{SEN55_HUMIDITY_OFFSET}/state")),
        "dps310_pressure_offset": _to_float(_val(snapshot, f"number/{DPS310_PRESSURE_OFFSET}/state")),
        "seen_at": {
            "prevent_sleep": _seen(snapshot, f"switch/{PREVENT_SLEEP}/state"),
            "led_brightness": _seen(snapshot, f"number/{LED_BRIGHTNESS}/state"),
            "led_alarm_mode": _seen(snapshot, f"switch/{LED_ALARM_MODE}/state"),
            "sleep_duration_min": _seen(snapshot, f"number/{SLEEP_DURATION}/state"),
            "sen55_temperature_offset": _seen(snapshot, f"number/{SEN55_TEMPERATURE_OFFSET}/state"),
            "sen55_humidity_offset": _seen(snapshot, f"number/{SEN55_HUMIDITY_OFFSET}/state"),
            "dps310_pressure_offset": _seen(snapshot, f"number/{DPS310_PRESSURE_OFFSET}/state"),
        },
    }


def publish_switch(object_id, on):
    _client.publish(f"{_topic_prefix()}/switch/{object_id}/command", "ON" if on else "OFF")


def publish_number(object_id, value):
    _client.publish(f"{_topic_prefix()}/number/{object_id}/command", str(value))


def publish_button(object_id):
    _client.publish(f"{_topic_prefix()}/button/{object_id}/command", "PRESS")


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
