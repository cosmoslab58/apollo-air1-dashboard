"""Unit tests for the day/night LED ramp readback in mqtt_bridge.

The property under test is that the app reports the ramp only when the device
actually told it where the ramp is. Every other outcome -- no snapshot yet,
firmware too old to send the fields, a clock that has not synced, a garbled
payload -- has to come back as None, because the alternative is the app doing
its own sun arithmetic and eventually disagreeing with the light on the wall.
That disagreement is the specific failure this dashboard is built to avoid (see
the band-table plumbing in bands.py for the same argument about colours).

No broker is touched: the parser is fed a snapshot dict directly.
"""
import json

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
