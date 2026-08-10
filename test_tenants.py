"""Tenant and device selection: resolution rules, the InfluxDB filter, and the
boundary between what a second home can show and what this container can act on.

The live deployment only has one tenant, so every multi-tenant test here fakes
a second one by patching tenants.known() -- which is also the seam a real
second home would arrive through, since the list comes from InfluxDB tag
values rather than config.

The boundary moved when the container stopped being wired to a single sensor:
it is the HOUSE now, not the device. Live state and controls follow whichever
device is selected inside our own house; only another house, or "All devices"
(nothing to aim a command at), is refused. The tests below are written to fail
if either half of that regresses -- a control silently reaching the default
sensor instead of the selected one is invisible at QoS 0."""
import json

import pytest

import app as app_module
import bands
import home_config
import influx
import mqtt_bridge
import purpleair
import tenants

OTHER = "maple"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Pin the settings these tests read, rather than inheriting a local .env.
    CI has no .env, so anything depending on one passes only on a developer's
    machine -- which is exactly how this file first went green locally and red
    on the runner. Also clears the per-request tenant and device afterwards so
    a value leaked from one test can't decide another's.

    Deliberately the LEGACY `MQTT_TOPIC_PREFIX` form: the deployed env still
    carries it, so the whole file doubles as the back-compat suite for deriving
    the house from `<house>/<device>`. The house-prefix settings are cleared
    rather than left alone, or a .env that has moved on would silently take
    over and this file would be testing a different house than it says."""
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "cosmoslab/air1")
    monkeypatch.delenv("MQTT_HOUSE_PREFIX", raising=False)
    monkeypatch.delenv("MQTT_DEVICE", raising=False)
    monkeypatch.setenv("INFLUX_BUCKET", "air_quality")
    monkeypatch.delenv("TENANT_LABELS", raising=False)
    yield
    tenants.set_current(None)
    tenants.set_current_device(None)


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


def test_the_house_prefix_is_the_modern_setting(monkeypatch):
    # What the env moves to once a house has more than one room: the container
    # is wired to the house, and no device is named at all.
    monkeypatch.delenv("MQTT_TOPIC_PREFIX", raising=False)
    monkeypatch.setenv("MQTT_HOUSE_PREFIX", "walnut")
    assert tenants.own() == "walnut"
    assert tenants.own_device() == ""


def test_the_house_prefix_wins_over_the_legacy_pair(monkeypatch):
    # Both set is the state of a half-migrated env. The new setting has to win,
    # or updating it would appear to do nothing.
    monkeypatch.setenv("MQTT_TOPIC_PREFIX", "oldhouse/oldsensor")
    monkeypatch.setenv("MQTT_HOUSE_PREFIX", "walnut")
    monkeypatch.setenv("MQTT_DEVICE", "living_room")
    assert tenants.own() == "walnut"
    assert tenants.own_device() == "living_room"


def test_a_house_is_one_segment(monkeypatch):
    # Node-RED reads the tenant as parts[0] of the topic. A prefix with a slash
    # in it would put the dashboard on a different tenant id than the ingestion
    # flow tags the data with, and the two would never meet.
    monkeypatch.setenv("MQTT_HOUSE_PREFIX", "walnut/living_room")
    assert tenants.own() == "walnut"


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
    monkeypatch.setattr(mqtt_bridge, "get_raw", lambda _d, _t: json.dumps({"air_band": 4}))
    monkeypatch.setattr(mqtt_bridge, "get_seen", lambda _d, _t: 12345)
    body = client.get(f"/api/tick?tenant={other}").get_json()
    # Nulls already mean "no information" to the frontend, which falls back to
    # its own timer -- the alternative is attaching our device's live band to
    # someone else's readings.
    assert body["seen_at"] is None and body["air_band"] is None
    # Our own home still reports live.
    assert client.get("/api/tick").get_json()["air_band"] == 4


def test_band_table_is_not_borrowed_across_homes(client, two_tenants, monkeypatch):
    _, other = two_tenants
    monkeypatch.setattr(bands, "get_table", lambda device=None: {"co2_ppm": [[1000, 1]]})
    assert client.get("/api/bands").status_code == 200
    res = client.get(f"/api/bands?tenant={other}")
    assert res.status_code == 404
    assert "band table" in res.get_json()["error"]


def test_device_controls_refuse_another_home(client, two_tenants, monkeypatch):
    _, other = two_tenants
    monkeypatch.setattr(mqtt_bridge, "available", lambda: True)
    monkeypatch.setattr(mqtt_bridge, "device_online", lambda d: True)
    published = []
    monkeypatch.setattr(mqtt_bridge, "publish_switch",
                        lambda d, o, s: published.append((d, o, s)))

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
    # Deliberately a DEAD device name on the legacy `MQTT_TOPIC_PREFIX` setting.
    # `aqm_alpha` was renamed to `living_room` on 2026-08-09 and no longer exists
    # anywhere in the live system -- which is the point. This test covers the
    # back-compat fallback in own()/own_device(), so using a name the current
    # deployment still uses would let a regression pass by accident. Do not
    # "fix" this to `living_room`; see the same note on the fixture above.
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


def test_ownership_is_the_house_plus_one_selected_device(two_devices):
    own, other = two_devices
    tenants.set_current(own)
    # Any sensor in our own house is ours to control -- that is what house
    # scoping means, and it is why one container now serves every room.
    tenants.set_current_device("aqm_beta")
    assert tenants.is_own() is True
    assert tenants.is_own_house() is True
    # "All devices" is our house but not one sensor: a command cannot be aimed
    # at every room at once, and no single sensor's live band speaks for the
    # house. is_own_house stays true -- house-level writes are still fine.
    tenants.set_current_device(None)
    assert tenants.is_own() is False
    assert tenants.is_own_house() is True
    # Another house is out of reach at both levels: no broker grant there, and
    # a publish would be denied silently at QoS 0.
    tenants.set_current(other)
    tenants.set_current_device("aqm_gamma")
    assert tenants.is_own() is False
    assert tenants.is_own_house() is False


def test_control_device_is_the_selected_one_never_a_default(two_devices):
    own, _ = two_devices
    tenants.set_current(own)
    tenants.set_current_device("aqm_beta")
    # Substituting own_device() here would publish a command aimed at one room
    # into another, and at QoS 0 nothing would ever report the difference.
    assert tenants.control_device() == "aqm_beta"
    assert tenants.control_device() != tenants.own_device()
    tenants.set_current_device(None)
    assert tenants.control_device() is None


def test_controls_reach_a_different_device_in_our_own_home(client, two_devices, monkeypatch):
    """The point of house scoping: N rooms, one container.

    This asserts the command carries the SELECTED device, not merely that it
    returned 200 -- publishing to the default sensor's topic would look
    identical from the caller's side and land in the wrong room."""
    monkeypatch.setattr(mqtt_bridge, "available", lambda: True)
    online = []
    monkeypatch.setattr(mqtt_bridge, "device_online", lambda d: online.append(d) or True)
    published = []
    monkeypatch.setattr(mqtt_bridge, "publish_switch",
                        lambda d, o, s: published.append((d, o, s)))

    res = client.post(f"/api/control/switch/{mqtt_bridge.PREVENT_SLEEP}?device=aqm_beta",
                      json={"state": True})
    assert res.status_code == 200
    assert published == [("aqm_beta", mqtt_bridge.PREVENT_SLEEP, True)]
    # Reachability is asked about that same sensor: rooms deep-sleep on
    # independent cycles, so another one being awake answers nothing.
    assert online == ["aqm_beta"]


def test_every_control_kind_carries_the_selected_device(client, two_devices, monkeypatch):
    """All four publish helpers, because they are four separate call sites and
    only one of them was covered above."""
    monkeypatch.setattr(mqtt_bridge, "available", lambda: True)
    monkeypatch.setattr(mqtt_bridge, "device_online", lambda d: True)
    seen = []
    monkeypatch.setattr(mqtt_bridge, "publish_number", lambda d, o, v: seen.append(d))
    monkeypatch.setattr(mqtt_bridge, "publish_select", lambda d, o, p: seen.append(d))
    monkeypatch.setattr(mqtt_bridge, "publish_button", lambda d, o: seen.append(d))

    assert client.post(f"/api/control/number/{mqtt_bridge.LED_BRIGHTNESS}?device=aqm_beta",
                       json={"value": 40}).status_code == 200
    assert client.post(f"/api/control/select/{mqtt_bridge.LED_AMBIENCE}?device=aqm_beta",
                       json={"option": "Breathing"}).status_code == 200
    assert client.post(f"/api/control/button/{mqtt_bridge.ESP_REBOOT}?device=aqm_beta"
                       ).status_code == 200
    assert seen == ["aqm_beta", "aqm_beta", "aqm_beta"]


def test_controls_refuse_the_all_devices_view(client, two_devices, monkeypatch):
    monkeypatch.setattr(mqtt_bridge, "available", lambda: True)
    monkeypatch.setattr(mqtt_bridge, "device_online", lambda d: True)
    published = []
    monkeypatch.setattr(mqtt_bridge, "publish_switch",
                        lambda d, o, s: published.append((d, o, s)))

    # An explicit empty ?device= is the picker's All-devices option.
    res = client.post(f"/api/control/switch/{mqtt_bridge.PREVENT_SLEEP}?device=",
                      json={"state": True})
    assert res.status_code == 409
    assert published == [], "a command was published with no device selected"
    # The message has to name the actual fix. "Only available for Walnut --
    # you're viewing Walnut" is the nonsense this branch exists to avoid.
    assert "one device selected" in res.get_json()["error"]
    assert client.get("/api/controls?device=").status_code == 409


def test_live_state_follows_the_selected_device(client, two_devices, monkeypatch):
    """/api/tick and /api/controls read one sensor's slice of the bridge cache.

    The cache is keyed by (device, suffix) now; serving the default sensor's
    snapshot under another room's name would attach one room's live band to
    another's readings -- the same lie the cross-tenant check prevents."""
    monkeypatch.setattr(mqtt_bridge, "get_raw",
                        lambda d, t: json.dumps({"air_band": 4}) if d == "aqm_beta" else None)
    monkeypatch.setattr(mqtt_bridge, "get_seen", lambda d, t: 999 if d == "aqm_beta" else None)
    monkeypatch.setattr(mqtt_bridge, "get_state", lambda d: {"online": True, "device": d})

    body = client.get("/api/tick?device=aqm_beta").get_json()
    assert body["air_band"] == 4 and body["seen_at"] == 999
    assert client.get("/api/controls?device=aqm_beta").get_json()["device"] == "aqm_beta"

    # The default sensor has published nothing, and says so rather than
    # borrowing the other room's snapshot.
    quiet = client.get(f"/api/tick?device={tenants.own_device()}").get_json()
    assert quiet["air_band"] is None and quiet["seen_at"] is None

    # All devices: no single sensor to report, and nulls already mean "no
    # information" to the frontend.
    both = client.get("/api/tick?device=").get_json()
    assert both["air_band"] is None and both["seen_at"] is None


def test_band_table_follows_the_selected_device(client, two_devices, monkeypatch):
    """Each sensor publishes its own table (they can be on different firmware),
    so the table has to come from the sensor on screen -- serving another's is
    the invented-default this app refuses to do."""
    asked = []
    monkeypatch.setattr(bands, "get_table",
                        lambda device=None: asked.append(device) or {"co2": [1000]})
    assert client.get("/api/bands?device=aqm_beta").status_code == 200
    assert asked == ["aqm_beta"]
    # ...and there is no house-wide answer to give.
    res = client.get("/api/bands?device=")
    assert res.status_code == 404
    assert "band table" in res.get_json()["error"]


def test_the_home_location_is_a_house_level_write(client, two_devices, monkeypatch):
    """Regression guard for the weaker check: the home location belongs to the
    house (outside_air_quality has no device_id at all), so it must not be
    gated on which room is selected -- including "All devices"."""
    saved = []
    monkeypatch.setattr(app_module, "_resolve_zip",
                        lambda z: {"lat": 42.6, "lon": -83.4, "reporting_area": "Orchard Lake, MI"})
    monkeypatch.setattr(purpleair, "nearest_outdoor_sensor", lambda lat, lon: None)
    monkeypatch.setattr(home_config, "set_home",
                        lambda **kw: (saved.append(kw) or (kw, True)))
    assert client.post("/api/home?device=", json={"zip": "48324"}).status_code == 200
    assert client.post("/api/home?device=aqm_beta", json={"zip": "48324"}).status_code == 200
    assert len(saved) == 2


def test_device_picker_hidden_with_one_device(client, two_tenants, monkeypatch):
    monkeypatch.setattr(tenants, "known_devices",
                        lambda tenant=None, force=False: (tenants.own_device(),))
    assert 'id="device-select"' not in client.get("/").data.decode()


def test_device_picker_appears_with_two(client, two_devices):
    page = client.get("/").data.decode()
    assert 'id="device-select"' in page
    assert "aqm_beta" in page
    assert "All devices" in page


def test_every_room_in_our_house_is_offered_as_controllable(two_devices):
    """The picker's third element drives the "(read-only)" suffix. While this
    container was pinned to one sensor it marked every other room read-only,
    which is now simply false -- controllability is a property of the house."""
    own, other = two_devices
    tenants.set_current(own)
    assert [c for _, _, c in tenants.device_choices()] == [True, True]
    # ...and no room in someone else's house is.
    tenants.set_current(other)
    assert not any(c for _, _, c in tenants.device_choices())


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
