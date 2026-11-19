"""Unit tests for the home/away config store (home_config.py): default home,
slugify, the set/clear round-trip against a temp data dir, and that saving a
home publishes the record to automation (the retained MQTT config) while away
never does. mqtt_bridge.publish_config is monkeypatched -- no broker is touched."""
import importlib

import pytest


@pytest.fixture
def hc(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AIRNOW_ZIP", "48324")
    import home_config
    importlib.reload(home_config)
    # Default: pretend the broker link is up and record publishes.
    monkeypatch.setattr(home_config.mqtt_bridge, "publish_config",
                        lambda topic, payload, retain=True: True)
    return home_config


def test_default_home_when_no_file(hc):
    # Default reflects the real current home (automation's fallback literals), so
    # the app display is truthful before any save.
    home = hc.get_home()
    assert home["zip"] == "48324"
    assert home["location_slug"] == "orchard_lake"
    assert home["purpleair_sensor"] == 178257
    assert hc.get_away() is None


def test_slugify(hc):
    assert hc.slugify("Orchard Lake, MI", "fb") == "orchard_lake_mi"
    assert hc.slugify("", "zip_48324") == "zip_48324"
    assert hc.slugify("!!!", "fb") == "fb"


def test_set_home_persists_and_publishes(hc, monkeypatch):
    calls = []
    monkeypatch.setattr(hc.mqtt_bridge, "publish_config",
                        lambda topic, payload, retain=True: calls.append((topic, payload)) or True)
    home, published = hc.set_home(zip_code="90210", lat=34.1, lon=-118.4,
                                  reporting_area="Beverly Hills, CA", purpleair_sensor=123)
    assert published is True
    assert home["location_slug"] == "beverly_hills_ca"
    assert home["purpleair_sensor"] == 123
    assert hc.get_home()["zip"] == "90210"  # persisted
    # The published payload is the poll record automation needs -- coords + slug +
    # sensor, no reporting_area.
    topic, payload = calls[0]
    assert payload["lat"] == 34.1 and payload["location_slug"] == "beverly_hills_ca"
    assert payload["purpleair_sensor"] == 123
    assert "reporting_area" not in payload


def test_set_home_label_wins_slug(hc):
    home, _ = hc.set_home(zip_code="90210", lat=1, lon=2,
                          reporting_area="Beverly Hills, CA", purpleair_sensor=None,
                          label="Grandma's House")
    assert home["location_slug"] == "grandma_s_house"


def test_set_home_still_saves_when_broker_down(hc, monkeypatch):
    monkeypatch.setattr(hc.mqtt_bridge, "publish_config", lambda *a, **k: False)
    home, published = hc.set_home(zip_code="90210", lat=1, lon=2,
                                  reporting_area="X, CA", purpleair_sensor=None)
    assert published is False
    assert hc.get_home()["zip"] == "90210"  # file written regardless


def test_away_set_and_clear_never_publishes(hc, monkeypatch):
    calls = []
    monkeypatch.setattr(hc.mqtt_bridge, "publish_config",
                        lambda *a, **k: calls.append(1) or True)
    away = hc.set_away(zip_code="10001", lat=40.7, lon=-74.0, reporting_area="New York, NY")
    assert away["lat"] == 40.7
    assert hc.get_away()["zip"] == "10001"
    hc.clear_away()
    assert hc.get_away() is None
    assert calls == []  # away is never published to automation by design


def test_republish_home_noop_without_saved_file(hc, monkeypatch):
    calls = []
    monkeypatch.setattr(hc.mqtt_bridge, "publish_config", lambda *a, **k: calls.append(1) or True)
    # No home.json saved yet -> the seeded default is never auto-published.
    assert hc.republish_home() is False
    assert calls == []


def test_republish_home_publishes_saved_home(hc, monkeypatch):
    hc.set_home(zip_code="90210", lat=1.0, lon=2.0, reporting_area="X, CA", purpleair_sensor=9)
    calls = []
    monkeypatch.setattr(hc.mqtt_bridge, "publish_config", lambda *a, **k: calls.append(1) or True)
    assert hc.republish_home() is True  # a real saved home does re-publish
    assert len(calls) == 1
