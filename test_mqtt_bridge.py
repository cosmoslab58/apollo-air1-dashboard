"""Unit tests for the day/night LED ramp readback in mqtt_bridge.

The property under test is that the app reports the ramp only when the device
actually told it where the ramp is. Every other outcome -- no snapshot yet,
firmware too old to send the fields, a clock that has not synced, a garbled
payload -- has to come back as None, because the alternative is the app doing
its own sun arithmetic and eventually disagreeing with the light on the wall.
That disagreement is the specific failure this dashboard is built to avoid (see
the band-table plumbing in bands.py for the same argument about colours).

No broker is touched: the parser is fed a snapshot dict directly, and the
cache tests below hand messages straight to the on_message callback.
"""
import json

import pytest

import mqtt_bridge


def _snap(state_payload):
    """A bridge cache holding `state_payload` on the device's snapshot topic."""
    raw = state_payload if isinstance(state_payload, str) else json.dumps(state_payload)
    return {"state": {"value": raw, "seen_at": 0.0}}


def test_reports_effective_brightness_and_elevation_from_the_snapshot():
    out = mqtt_bridge._led_ramp_readback(_snap({
        "co2_ppm": 812.0,
        "led_brightness_effective_pct": 37,
        "sun_elevation_deg": -2.4,
    }))
    assert out == {"led_brightness_effective": 37, "sun_elevation_deg": -2.4}


def test_no_snapshot_yet_is_unknown_not_zero():
    assert mqtt_bridge._led_ramp_readback({}) == {
        "led_brightness_effective": None,
        "sun_elevation_deg": None,
    }


def test_older_firmware_reports_neither_field():
    """A snapshot from before the ramp existed is a valid snapshot, not an
    error -- it simply has nothing to say about the LED."""
    out = mqtt_bridge._led_ramp_readback(_snap({"co2_ppm": 812.0, "air_band": 0}))
    assert out == {"led_brightness_effective": None, "sun_elevation_deg": None}


def test_unsynced_clock_omits_elevation_but_still_reports_brightness():
    """The firmware drops sun_elevation_deg rather than sending a 0 that would
    read as dusk. The brightness is still real -- it is the daytime level the
    ramp holds while the clock is untrusted -- so it must survive."""
    out = mqtt_bridge._led_ramp_readback(_snap({"led_brightness_effective_pct": 100}))
    assert out == {"led_brightness_effective": 100, "sun_elevation_deg": None}


def test_not_yet_computed_sentinel_is_unknown():
    """-1 is the firmware's "no value yet" marker for the first seconds after
    boot. Passing it through would render as a nonsensical -1% brightness."""
    out = mqtt_bridge._led_ramp_readback(_snap({"led_brightness_effective_pct": -1}))
    assert out["led_brightness_effective"] is None


def test_zero_percent_is_a_real_reading_and_survives():
    """The one value that must NOT be filtered as falsy: 0 is a deliberately
    dark LED (night brightness 0), which is a supported configuration."""
    out = mqtt_bridge._led_ramp_readback(_snap({
        "led_brightness_effective_pct": 0,
        "sun_elevation_deg": -11.2,
    }))
    assert out == {"led_brightness_effective": 0, "sun_elevation_deg": -11.2}


def test_zero_elevation_is_a_real_reading_and_survives():
    """0° is the horizon -- mid-ramp, the most interesting moment there is."""
    out = mqtt_bridge._led_ramp_readback(_snap({
        "led_brightness_effective_pct": 50,
        "sun_elevation_deg": 0,
    }))
    assert out["sun_elevation_deg"] == 0


def test_malformed_payloads_degrade_to_unknown():
    for payload in ("not json at all", "[1, 2, 3]", '"a string"', "null"):
        assert mqtt_bridge._led_ramp_readback(_snap(payload)) == {
            "led_brightness_effective": None,
            "sun_elevation_deg": None,
        }


def test_wrong_types_degrade_to_unknown():
    """Booleans included: `True` is an int in Python and would otherwise slip
    through the numeric check and render as 1%."""
    out = mqtt_bridge._led_ramp_readback(_snap({
        "led_brightness_effective_pct": True,
        "sun_elevation_deg": "dusk",
    }))
    assert out == {"led_brightness_effective": None, "sun_elevation_deg": None}


# ---------- one subscription, a whole house ----------
#
# The bridge subscribes to `<house>/#` and keys everything it hears by
# (device, suffix). The failure these guard against is silent: with the old
# flat {suffix: ...} cache two rooms overwrite each other's `state` and
# `status`, so the dashboard reports whichever published last as both -- no
# error, no gap, just the wrong room's numbers under the right room's name.


class _Msg:
    """The two attributes paho's on_message callback actually reads."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


@pytest.fixture
def house(monkeypatch):
    monkeypatch.setenv("MQTT_HOUSE_PREFIX", "walnut")
    monkeypatch.delenv("MQTT_TOPIC_PREFIX", raising=False)
    monkeypatch.delenv("MQTT_DEVICE", raising=False)
    monkeypatch.setattr(mqtt_bridge, "_state", {})
    return "walnut"


def _deliver(topic, payload):
    mqtt_bridge._on_message(None, None, _Msg(topic, payload))


def test_two_rooms_do_not_overwrite_each_other(house):
    _deliver("walnut/living_room/state", '{"air_band": 1}')
    _deliver("walnut/bedroom/state", '{"air_band": 4}')
    assert mqtt_bridge.get_raw("living_room", "state") == '{"air_band": 1}'
    assert mqtt_bridge.get_raw("bedroom", "state") == '{"air_band": 4}'
    # An unheard-of room is None, not the last thing anyone said.
    assert mqtt_bridge.get_raw("kitchen", "state") is None
    assert mqtt_bridge.get_seen("kitchen", "state") is None


def test_online_is_per_device(house):
    """Rooms deep-sleep on independent cycles, so one being on the broker says
    nothing about another -- and this gates whether a command is sent at all."""
    _deliver("walnut/living_room/status", "online")
    _deliver("walnut/bedroom/status", "offline")
    assert mqtt_bridge.device_online("living_room") is True
    assert mqtt_bridge.device_online("bedroom") is False
    assert mqtt_bridge.device_online("kitchen") is False


def test_multi_segment_suffixes_stay_whole(house):
    """`switch/prevent_sleep/state` is one key. Splitting on every slash would
    scatter each control entity across three levels of nothing."""
    _deliver("walnut/bedroom/switch/prevent_sleep/state", "ON")
    assert mqtt_bridge.get_state("bedroom")["prevent_sleep"] is True
    # ...and belongs to that room alone.
    assert mqtt_bridge.get_state("living_room")["prevent_sleep"] is False


def test_topics_outside_the_house_are_ignored(house):
    # The ACL should stop these ever arriving, but a wildcard subscription is
    # exactly the place not to trust that.
    _deliver("maple/living_room/state", '{"air_band": 5}')
    _deliver("walnut", "bare")
    _deliver("walnut/orphan", "no suffix")
    assert mqtt_bridge._state == {}


def test_command_topics_name_the_device(house):
    assert mqtt_bridge._command_topic("bedroom", "switch", "prevent_sleep") == (
        "walnut/bedroom/switch/prevent_sleep/command"
    )
