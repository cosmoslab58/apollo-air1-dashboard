"""Unit tests for the band table store (bands.py).

The property under test throughout is that this app never invents thresholds.
It serves what the device published, or the last thing the device published, or
nothing -- and "nothing" has to stay a supported answer, because the alternative
(a plausible built-in default) is exactly how the page and the LED drifted apart
in the first place. mqtt_bridge.get_raw is monkeypatched; no broker is touched.
"""
import importlib
import json

import pytest

# A structurally valid table, shaped like the firmware's publish_band_config
# payload. Values mirror the shipping band_cuts_* globals.
GOOD = {
    "compare": "lte",
    "bands": 6,
    "aqi": [50, 100, 150, 200, 300],
    "co2": [800, 1100, 2000, 3500, 5000],
    "voc": [150, 250, 400],
    "nox": [20, 150, 250, 400],
    "colors": ["#00c000", "#c0c000", "#e08000", "#e00000", "#9900ff", "#600000"],
    "elevated_band": 1,
}


@pytest.fixture
def bd(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import bands
    importlib.reload(bands)
    monkeypatch.setattr(bands.mqtt_bridge, "get_raw", lambda suffix: None)
    return bands


def _serve(bd, monkeypatch, payload):
    """Pretend the retained topic currently holds `payload` (str or dict)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(bd.mqtt_bridge, "get_raw", lambda suffix: raw)


def test_none_when_never_seen(bd):
    # No live topic, no cache file: the honest answer is "no table", which the
    # API turns into a 404 and the frontend into uncoloured readings.
    assert bd.get_table() is None


def test_live_table_is_served_and_cached(bd, monkeypatch):
    _serve(bd, monkeypatch, GOOD)
    assert bd.get_table() == GOOD
    # Persisted, so a broker outage at next boot doesn't strip every colour.
    with open(bd.BANDS_FILE) as f:
        assert json.load(f) == GOOD


def test_cache_is_used_when_broker_silent(bd, monkeypatch):
    _serve(bd, monkeypatch, GOOD)
    bd.get_table()
    monkeypatch.setattr(bd.mqtt_bridge, "get_raw", lambda suffix: None)
    assert bd.get_table() == GOOD


def test_live_table_wins_over_stale_cache(bd, monkeypatch):
    _serve(bd, monkeypatch, GOOD)
    bd.get_table()
    # A firmware change retunes CO2 and republishes the retained topic. Serving
    # the cached copy here would leave the page banding on numbers the device
    # has stopped using -- the exact drift this module prevents.
    retuned = dict(GOOD, co2=[900, 1200, 2000, 3500, 5000])
    _serve(bd, monkeypatch, retuned)
    assert bd.get_table()["co2"] == [900, 1200, 2000, 3500, 5000]
    with open(bd.BANDS_FILE) as f:
        assert json.load(f)["co2"] == [900, 1200, 2000, 3500, 5000]


@pytest.mark.parametrize("bad,why", [
    ({**GOOD, "co2": [800, 800, 2000, 3500, 5000]}, "non-ascending cuts"),
    ({**GOOD, "voc": [150, "250", 400]}, "non-numeric cut"),
    ({**GOOD, "aqi": []}, "empty cuts"),
    ({k: v for k, v in GOOD.items() if k != "nox"}, "missing channel"),
    ({**GOOD, "colors": "green"}, "colors not a list"),
    ({**GOOD, "voc": [True, 250, 400]}, "bool masquerading as a number"),
    ([1, 2, 3], "not an object"),
])
def test_malformed_tables_are_rejected(bd, monkeypatch, bad, why):
    _serve(bd, monkeypatch, bad)
    assert bd.get_table() is None, why


def test_malformed_live_table_falls_back_to_cache(bd, monkeypatch):
    _serve(bd, monkeypatch, GOOD)
    bd.get_table()
    # A truncated or half-written retained payload must not wipe out working
    # colours; the last good table is better than none.
    _serve(bd, monkeypatch, {**GOOD, "aqi": []})
    assert bd.get_table() == GOOD


def test_non_json_payload_falls_back_to_cache(bd, monkeypatch):
    _serve(bd, monkeypatch, GOOD)
    bd.get_table()
    _serve(bd, monkeypatch, "not json at all")
    assert bd.get_table() == GOOD


def test_unreadable_cache_is_ignored(bd, monkeypatch):
    bd._save_cached(GOOD)
    with open(bd.BANDS_FILE, "w") as f:
        f.write("{ truncated")
    assert bd.get_table() is None


def test_partial_channel_table_is_all_or_nothing(bd, monkeypatch):
    # Half a table would colour some tiles from the device and leave others
    # grey, which reads as a rendering bug rather than a missing table.
    _serve(bd, monkeypatch, {k: v for k, v in GOOD.items() if k != "voc"})
    assert bd.get_table() is None
