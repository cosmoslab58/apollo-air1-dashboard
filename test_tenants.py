"""Tenant selection: resolution rules, the InfluxDB filter, and the boundary
between what a second home can show and what stays pinned to this container.

The live deployment only has one tenant, so every multi-tenant test here fakes
a second one by patching tenants.known() -- which is also the seam a real
second home would arrive through, since the list comes from InfluxDB tag
values rather than config."""
import json

import pytest

import app as app_module
import bands
import influx
import mqtt_bridge
import tenants

OTHER = "maple"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Pin the two settings these tests read, rather than inheriting a local
    .env. CI has no .env, so anything depending on one passes only on a
    developer's machine -- which is exactly how this file first went green
    locally and red on the runner. Also clears the per-request tenant
    afterwards so a value leaked from one test can't decide another's."""
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "cosmoslab/air1")
    monkeypatch.setenv("INFLUX_BUCKET", "air_quality")
    monkeypatch.delenv("TENANT_LABELS", raising=False)
    yield
    tenants.set_current(None)


@pytest.fixture
def two_tenants(monkeypatch, _hermetic_env):
    """Own tenant plus one other, without touching InfluxDB."""
    monkeypatch.setattr(tenants, "known", lambda force=False: (tenants.own(), OTHER))
    return tenants.own(), OTHER


# ---------- id validation and resolution ----------

@pytest.mark.parametrize("bad", [
    "", "UPPER", "has space", "-leading", "a" * 65, "x\" or true", "../etc", "a/b",
])
def test_bad_ids_are_rejected_by_the_pattern(bad):
    assert not tenants.ID_RE.match(bad)


def test_unknown_tenant_falls_back_to_own(two_tenants):
    own, _ = two_tenants
    assert tenants.resolve("nosuchhome", None) == own
    # Shape-valid but not a real tenant is the important one: the pattern
    # alone would let it through to Flux.
    assert tenants.resolve("plausible", None) == own


def test_query_arg_beats_cookie_beats_own(two_tenants):
    own, other = two_tenants
    assert tenants.resolve(other, None) == other
    assert tenants.resolve(None, other) == other
    # An explicit arg overrides a saved cookie, so a per-home link is shareable
    # without disturbing what the viewer had selected.
    assert tenants.resolve(own, other) == own
    assert tenants.resolve(None, None) == own


def test_own_tenant_is_the_first_topic_segment(monkeypatch):
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "somehome/air1")
    assert tenants.own() == "somehome"


def test_choices_put_the_controllable_home_first(two_tenants):
    own, _ = two_tenants
    ids = [c[0] for c in tenants.choices()]
    assert ids[0] == own
    assert all(len(c) == 3 for c in tenants.choices())


def test_labels_come_from_env_and_fall_back_to_the_id(monkeypatch):
    monkeypatch.setenv("TENANT_LABELS", "maple=Maple Street, cosmoslab = Walnut ")
    assert tenants.label("maple") == "Maple Street"
    assert tenants.label("cosmoslab") == "Walnut"
    assert tenants.label("unnamed_home") == "Unnamed Home"


def test_discovery_failure_still_offers_own_tenant(monkeypatch):
    monkeypatch.setattr(influx, "query_tenant_ids", lambda: (_ for _ in ()).throw(RuntimeError("influx down")))
    tenants._cache["at"] = 0.0
    tenants._cache["ids"] = ()
    # An InfluxDB outage must not empty the picker or raise -- every other read
    # on the page has its own error path already.
    assert tenants.known(force=True) == (tenants.own(),)


# ---------- the InfluxDB filter ----------

def test_every_query_is_scoped_to_one_tenant():
    tenants.set_current(OTHER)
    flux = influx._flux(["co2_ppm"], "-1h", measurement="air_quality")
    assert f'r.tenant == "{OTHER}"' in flux


def test_the_filter_follows_the_selected_tenant(two_tenants):
    own, other = two_tenants
    tenants.set_current(own)
    assert f'r.tenant == "{own}"' in influx._flux(["aqi"], "-1h", measurement="air_quality")
    tenants.set_current(other)
    assert f'r.tenant == "{other}"' in influx._flux(["aqi"], "-1h", measurement="air_quality")


def test_outside_queries_are_tenanted_too():
    # Regression: outside data is per-home as well, and it reaches InfluxDB
    # through the same builder -- if it ever stops doing so, two homes'
    # provider readings merge without any visible symptom.
    tenants.set_current(OTHER)
    flux = influx._flux(["aqi"], "-24h", measurement=influx.OUTSIDE_MEASUREMENT, sort=True)
    assert f'r.tenant == "{OTHER}"' in flux


# ---------- switching ----------

def test_switching_sets_a_cookie_and_reports_what_took_effect(client, two_tenants):
    _, other = two_tenants
    res = client.post("/api/tenant", json={"tenant": other})
    assert res.status_code == 200
    assert res.get_json()["tenant"] == other
    assert res.get_json()["is_own"] is False
    cookie = res.headers.get("Set-Cookie", "")
    assert f"{tenants.COOKIE}={other}" in cookie
    # Server-set and server-read: nothing in the browser needs to see it, and
    # the selection has to be known at render time anyway.
    assert "HttpOnly" in cookie
    # The cookie is what makes the choice stick across pages.
    assert "tenant-badge" in client.get("/").data.decode()


def test_switching_to_an_unknown_home_falls_back_rather_than_erroring(client, two_tenants):
    own, _ = two_tenants
    res = client.post("/api/tenant", json={"tenant": "nosuchhome"})
    assert res.status_code == 200
    # The caller is told which home actually took effect, so it can never be
    # left believing it switched to something it didn't.
    assert res.get_json()["tenant"] == own


def test_the_picker_is_hidden_with_only_one_home(client, monkeypatch):
    monkeypatch.setattr(tenants, "known", lambda force=False: (tenants.own(),))
    page = client.get("/").data.decode()
    assert 'id="tenant-select"' not in page


def test_the_picker_appears_once_a_second_home_exists(client, two_tenants):
    page = client.get("/").data.decode()
    assert 'id="tenant-select"' in page
    assert OTHER in page


def test_viewing_another_home_is_marked_read_only(client, two_tenants):
    _, other = two_tenants
    page = client.get(f"/?tenant={other}").data.decode()
    assert "tenant-badge" in page
    assert "read-only" in page
    # ...and is not claimed when looking at our own.
    assert "tenant-badge" not in client.get("/").data.decode()


# ---------- what stays pinned to this container ----------

def test_live_tick_reports_no_information_for_another_home(client, two_tenants, monkeypatch):
    _, other = two_tenants
    monkeypatch.setattr(mqtt_bridge, "get_raw", lambda _t: json.dumps({"air_band": 4}))
    monkeypatch.setattr(mqtt_bridge, "get_seen", lambda _t: 12345)
    body = client.get(f"/api/tick?tenant={other}").get_json()
    # Nulls already mean "no information" to the frontend, which falls back to
    # its own timer -- the alternative is attaching our device's live band to
    # someone else's readings.
    assert body["seen_at"] is None and body["air_band"] is None
    # Our own home still reports live.
    assert client.get("/api/tick").get_json()["air_band"] == 4


def test_band_table_is_not_borrowed_across_homes(client, two_tenants, monkeypatch):
    _, other = two_tenants
    monkeypatch.setattr(bands, "get_table", lambda: {"co2_ppm": [[1000, 1]]})
    assert client.get("/api/bands").status_code == 200
    res = client.get(f"/api/bands?tenant={other}")
    assert res.status_code == 404
    assert "band table" in res.get_json()["error"]


def test_device_controls_refuse_another_home(client, two_tenants, monkeypatch):
    _, other = two_tenants
    monkeypatch.setattr(mqtt_bridge, "available", lambda: True)
    monkeypatch.setattr(mqtt_bridge, "device_online", lambda: True)
    published = []
    monkeypatch.setattr(mqtt_bridge, "publish_switch", lambda o, s: published.append((o, s)))

    res = client.post(f"/api/control/switch/{mqtt_bridge.PREVENT_SLEEP}?tenant={other}",
                      json={"state": True})
    assert res.status_code == 409
    assert published == [], "a command reached the device while viewing another home"

    assert client.get(f"/api/controls?tenant={other}").status_code == 409
    # Still works for our own home.
    assert client.post(f"/api/control/switch/{mqtt_bridge.PREVENT_SLEEP}",
                       json={"state": True}).status_code == 200


# ---------- devices within a home ----------

@pytest.fixture
def two_devices(monkeypatch, two_tenants):
    """Own home with two sensors in it."""
    own, other = two_tenants
    monkeypatch.setattr(tenants, "known_devices",
                        lambda tenant=None, force=False:
                        (tenants.own_device(), "aqm_beta") if (tenant or own) == own else ("aqm_gamma",))
    return own, other


def test_own_device_is_the_second_topic_segment(monkeypatch):
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "walnut/aqm_alpha")
    assert tenants.own() == "walnut"
    assert tenants.own_device() == "aqm_alpha"


def test_device_scopes_the_indoor_query(two_devices):
    tenants.set_current_device("aqm_beta")
    flux = influx._flux(["co2_ppm"], "-1h", measurement=influx.MEASUREMENT)
    assert 'r.device_id == "aqm_beta"' in flux


def test_device_does_NOT_scope_outside_queries(two_devices):
    # outside_air_quality carries no device_id -- the readings belong to the
    # house. Filtering them by device matches nothing and blanks every outside
    # panel, which looks exactly like an upstream outage.
    tenants.set_current_device("aqm_beta")
    flux = influx._flux(["aqi"], "-24h", measurement=influx.OUTSIDE_MEASUREMENT, sort=True)
    assert "device_id" not in flux


def test_all_devices_drops_the_device_filter(two_devices):
    tenants.set_current_device(None)
    assert "device_id" not in influx._flux(["co2_ppm"], "-1h", measurement=influx.MEASUREMENT)


def test_empty_string_means_all_devices_not_fallback(two_devices):
    own, _ = two_devices
    tenants.set_current(own)
    # "" is how the picker's All-devices option asks for the house-wide view;
    # it must not be folded into "nothing was requested".
    assert tenants.resolve_device("", None) is None
    assert tenants.resolve_device(None, None) == tenants.own_device()


def test_a_device_from_another_home_is_never_carried_across(two_devices):
    own, other = two_devices
    tenants.set_current(other)
    # Showing our sensor's readings captioned with another house would be
    # actively misleading. The other home has exactly one sensor, so the
    # request resolves to THAT rather than widening to all devices -- a
    # single-sensor house should show its sensor.
    resolved = tenants.resolve_device(tenants.own_device(), None, tenant=other)
    assert resolved != tenants.own_device()
    assert resolved == "aqm_gamma"


def test_an_unknown_device_falls_back_to_our_own_in_our_own_home(two_devices):
    own, _ = two_devices
    tenants.set_current(own)
    # In our own house there IS an obvious default -- the sensor this container
    # is wired to -- so a stale id lands there rather than widening.
    assert tenants.resolve_device("aqm_nonexistent", None, tenant=own) == tenants.own_device()


def test_ownership_is_not_gated_by_a_device_when_none_is_configured(monkeypatch):
    # Regression: MQTT_TOPIC_PREFIX with no device segment (and CI, which has
    # no .env at all) left own_device() empty, which made is_own() false for
    # everything and turned unrelated 400s into 409s.
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "somehome")
    tenants.set_current("somehome")
    tenants.set_current_device(None)
    assert tenants.own_device() == ""
    assert tenants.is_own() is True


def test_controls_refuse_a_different_device_in_our_own_home(client, two_devices, monkeypatch):
    own, _ = two_devices
    monkeypatch.setattr(mqtt_bridge, "available", lambda: True)
    monkeypatch.setattr(mqtt_bridge, "device_online", lambda: True)
    published = []
    monkeypatch.setattr(mqtt_bridge, "publish_switch", lambda o, s: published.append(o))

    # Same house, different sensor: a command would still land on OUR device.
    res = client.post(f"/api/control/switch/{mqtt_bridge.PREVENT_SLEEP}?device=aqm_beta",
                      json={"state": True})
    assert res.status_code == 409
    assert published == []


def test_device_picker_hidden_with_one_device(client, two_tenants, monkeypatch):
    monkeypatch.setattr(tenants, "known_devices",
                        lambda tenant=None, force=False: (tenants.own_device(),))
    assert 'id="device-select"' not in client.get("/").data.decode()


def test_device_picker_appears_with_two(client, two_devices):
    page = client.get("/").data.decode()
    assert 'id="device-select"' in page
    assert "aqm_beta" in page
    assert "All devices" in page


def test_switching_home_does_not_carry_the_device_across(client, two_devices):
    own, other = two_devices
    res = client.post("/api/tenant", json={"tenant": other})
    assert res.status_code == 200
    # The other home has aqm_gamma, not our sensor.
    assert res.get_json()["device"] in ("aqm_gamma", "")


def test_changing_home_location_refuses_another_home(client, two_tenants):
    _, other = two_tenants
    # Would publish to this container's own retained config topic and move our
    # polling location from a page captioned with someone else's name.
    res = client.post(f"/api/home?tenant={other}", json={"zip": "48324"})
    assert res.status_code == 409
