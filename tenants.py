"""Which home -- and which sensor in it -- the dashboard is showing.

This container is *wired* to exactly one HOUSE. `MQTT_HOUSE_PREFIX` pins the
broker tree it subscribes to and publishes controls on (`<house>/#`), and the
broker ACL enforces that from the other side -- a topic outside the grant is
silently denied at QoS 0 (see the README's broker section). So switching
tenants here switches what is **read back from InfluxDB**: indoor history and
every outside provider, all of which Node-RED polls in and tags with `tenant`.
It does not, and cannot, move live device state or the control endpoints; those
stay inside `own()`.

The house, not the device, is the unit of wiring. It used to be the device --
`MQTT_TOPIC_PREFIX=<house>/<device>` pinned the container to one sensor, so a
second room meant a second container. Rooms are the identity now and a house
has several of them, so live state and controls follow the SELECTED device
anywhere inside our own house, and only a different house is out of reach.

That split is the whole design. Everything this app renders is a DB read
already -- even "current conditions" for the four outside providers, which
come back through influx.py rather than a live upstream call -- so tenanting
the DB layer tenants nearly the entire surface in one place.

Selection is resolved once per request into a ContextVar rather than threaded
through every call site. `influx._flux` is the only place that needs it, which
means the provider modules (airnow, google_aq, purpleair, owm) keep working
without knowing tenants exist at all. A ContextVar rather than a module global
because gunicorn runs one worker with eight threads here: a plain global would
let one viewer's tenant leak into another's concurrent request.
"""

import contextvars
import logging
import os
import re
import threading
import time

# A tenant id is the first segment of that home's MQTT topics and the value of
# the `tenant` tag on every point it logs.
#
# It gets interpolated into Flux, so it is checked twice before it can reach a
# query: against this pattern, and against the set of ids that actually exist.
# Anything failing either check falls back to own() rather than raising, so a
# stale cookie or a hand-edited ?tenant= degrades to this dashboard's own home
# instead of a 500 or, worse, a query with someone else's string spliced in.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

COOKIE = "tenant"
DEVICE_COOKIE = "device"
# A year: the picker is a "whose house am I looking at" preference, not
# session state, and a wall display should survive a reboot still showing the
# home it was set to.
COOKIE_MAX_AGE_S = 365 * 24 * 3600

_current = contextvars.ContextVar("current_tenant", default=None)
_current_device = contextvars.ContextVar("current_device", default=None)

# Discovery is a tag-values query against InfluxDB. Cached because otherwise
# every page render pays for it just to draw the picker, and the answer only
# changes when a whole new home is provisioned.
_TTL_S = 60
_lock = threading.Lock()
_cache = {"at": 0.0, "ids": ()}


def own():
    """The house this container is wired to, and the tenant id of its data.

    `MQTT_HOUSE_PREFIX` is the modern setting and is ONE topic segment: the
    same convention Node-RED uses when it takes the tenant id off
    `+/+/state` as `parts[0]`. Keeping one rule for "what is a tenant id"
    across both sides is what stops the dashboard and the ingestion flow from
    disagreeing about who a reading belongs to.

    Falls back to the first segment of the older `MQTT_TOPIC_PREFIX`
    (`<house>/<device>`) so a deployment whose env has not been updated yet
    keeps working unchanged -- the house was always its first segment, this
    just stops caring about the second. mqtt_bridge derives its subscription
    from this same function rather than re-reading the env, so the two can
    never disagree about which tree we are on."""
    house = os.environ.get("MQTT_HOUSE_PREFIX", "").strip("/")
    if house:
        return house.split("/")[0]
    return os.environ.get("MQTT_TOPIC_PREFIX", "").split("/")[0]


def set_current(tenant):
    _current.set(tenant or None)


def current():
    """The tenant being viewed. Defaults to own() outside a request, so
    anything running off the request path (startup, background work) reads
    this container's own data rather than whatever was viewed last."""
    return _current.get() or own()


def own_device():
    """This container's DEFAULT device, or "" when it has no opinion.

    No longer a limit -- controls reach any device in our own house now. It is
    only (a) which sensor a fresh visitor lands on, and (b) the one entry
    known_devices() can offer while InfluxDB is unreachable or has not logged
    the device yet. That second job is why it still matters: without it, an
    InfluxDB outage would empty the device list, leave nothing selected, and
    take the *broker* controls down with the *database*, which are supposed to
    be independent failure domains.

    `MQTT_DEVICE` is the modern setting; the second segment of the older
    `MQTT_TOPIC_PREFIX` (`<house>/<device>`) is honored for back-compat, which
    is what makes an un-updated env behave exactly as it did before. Empty is a
    perfectly good answer for a house whose sensors are all discovered from
    data."""
    device = os.environ.get("MQTT_DEVICE", "").strip("/")
    if device:
        return device
    parts = os.environ.get("MQTT_TOPIC_PREFIX", "").split("/")
    return parts[1] if len(parts) > 1 else ""


def set_current_device(device):
    _current_device.set(device or None)


def current_device():
    """The device being viewed, or None for "every device in this home".

    None is a real answer, not a missing one: the outside measurements have no
    device at all, and a house-level view across two sensors is meaningful."""
    return _current_device.get()


def is_own(tenant=None):
    """True when the viewed home is the one this container is wired to AND a
    single device is in view. The gate for live state and device controls.

    Two separate refusals, for two different reasons:

    - Another HOUSE: we hold no broker grant there. A command published to it
      is silently denied at QoS 0 and its device never reports state to us, so
      there is nothing honest to show or send.
    - "All devices" (current_device() is None): our own house, but a command
      cannot be aimed at every sensor at once, and one sensor's live band is
      not the house's. Refusing is the only truthful answer; the fix is to
      pick a room in the device picker.

    A DIFFERENT device in our own house is emphatically fine now -- that is the
    whole point of house scoping. is_own_house() below is the weaker check for
    things that genuinely belong to the house rather than to a sensor."""
    return is_own_house(tenant) and current_device() is not None


def is_own_house(tenant=None):
    """True when the viewed home is ours, whichever device is selected.

    The gate for house-level writes -- the home location, which is one place
    for every sensor in it (see home_config.CONFIG_TOPIC) and is meaningful
    even while the view is "all devices"."""
    return (tenant or current()) == own()


def control_device():
    """The device live state and control publishes act on, or None.

    Exactly the selected device: there is deliberately no fall back to
    own_device() here. With a device dimension in play, quietly substituting
    the default sensor for the one on screen is how a command aimed at the
    bedroom lands in the living room -- and at QoS 0 nothing would ever say
    so. None means "refuse", and is_own() is the check that turns it into a
    409 before any topic gets built."""
    return current_device()


def _labels():
    """Optional display names: TENANT_LABELS="cosmoslab=Walnut,maple=Maple St".

    Ids are internal (they are account names and topic segments); the picker
    should say where a house is. Unset is fine -- label() falls back to the id
    itself, so a new tenant appears immediately and can be named later."""
    out = {}
    for part in os.environ.get("TENANT_LABELS", "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def label(tenant):
    return _labels().get(tenant) or tenant.replace("_", " ").replace("-", " ").title()


def known(force=False):
    """Tenant ids that have data, plus own().

    own() is always included even with no points yet, so a freshly provisioned
    home is selectable before Node-RED has logged anything for it -- and so the
    picker is never empty when InfluxDB is unreachable. A discovery failure is
    logged and degrades to just own(); it must not take the dashboard down,
    since every other read has its own error path already."""
    now = time.monotonic()
    with _lock:
        if not force and _cache["ids"] and now - _cache["at"] < _TTL_S:
            return _cache["ids"]

    ids = set()
    try:
        # Imported here, not at module scope: influx imports this module to
        # read current(), so a top-level import either way round would cycle.
        import influx
        ids = {i for i in influx.query_tenant_ids() if ID_RE.match(i)}
    except Exception:
        logging.exception("tenant discovery failed; offering this dashboard's own tenant only")

    mine = own()
    if mine:
        ids.add(mine)
    result = tuple(sorted(ids))
    with _lock:
        _cache["at"], _cache["ids"] = now, result
    return result


def resolve(requested, cookie_value):
    """Pick the tenant for this request: explicit ?tenant= beats the saved
    cookie, which beats own().

    The query arg wins so a link can point at one specific home without
    disturbing what the viewer has selected, which is what makes a per-home
    URL shareable."""
    for candidate in (requested, cookie_value):
        if candidate and ID_RE.match(candidate) and candidate in known():
            return candidate
    return own()


def choices():
    """(id, label, is_own) for the picker, own tenant first then alphabetical
    -- the one you can actually control belongs at the top."""
    mine = own()
    return [
        (tenant, label(tenant), tenant == mine)
        for tenant in sorted(known(), key=lambda t: (t != mine, t))
    ]


# Devices are cached per tenant on the same short TTL and for the same reason
# as the tenant list: it is a tag-values query and the answer only changes when
# hardware is added.
_device_cache = {}


def known_devices(tenant=None, force=False):
    """Device ids that have logged data for one home, plus own_device() when
    that home is ours -- so a newly flashed sensor is selectable before it has
    written anything.

    Scoped to the tenant on purpose: device ids are only unique within a house
    (two homes may each run a `living_room`), so a global device list would
    offer a device that has no data under the selected home."""
    tenant = tenant or current()
    now = time.monotonic()
    with _lock:
        hit = _device_cache.get(tenant)
        if hit and not force and now - hit[0] < _TTL_S:
            return hit[1]

    ids = set()
    try:
        import influx
        ids = {i for i in influx.query_device_ids(tenant) if ID_RE.match(i)}
    except Exception:
        logging.exception("device discovery failed for tenant %s", tenant)

    if tenant == own() and own_device():
        ids.add(own_device())
    result = tuple(sorted(ids))
    with _lock:
        _device_cache[tenant] = (now, result)
    return result


def resolve_device(requested, cookie_value, tenant=None):
    """Pick the device for this request, or None for "all devices".

    The empty string is a meaningful request -- it is how the picker's "All
    devices" option asks for the house-wide view -- so it is distinguished from
    absent rather than folded into the fallback. A device that is not in the
    selected home falls back to all-devices instead of this container's own
    sensor: silently showing our device's readings under another house's name
    is the one answer that would be actively misleading."""
    tenant = tenant or current()
    devices = known_devices(tenant)
    for candidate in (requested, cookie_value):
        if candidate == "":
            return None
        if candidate and ID_RE.match(candidate) and candidate in devices:
            return candidate
    if tenant == own() and own_device() in devices:
        return own_device()
    return devices[0] if len(devices) == 1 else None


def device_choices(tenant=None):
    """(id, label, controllable) for the device picker, default device first.

    `controllable` is a property of the HOUSE, not of the individual sensor:
    every device in our own house can be controlled from here, so the picker
    must not mark all but one of them "(read-only)" the way it did while this
    container was pinned to a single sensor. Ordering still floats
    own_device() to the top -- it is the one a fresh visitor lands on, so it
    belongs where they look first."""
    tenant = tenant or current()
    mine = own_device() if tenant == own() else None
    controllable = tenant == own()
    return [
        (device, label(device), controllable)
        for device in sorted(known_devices(tenant), key=lambda d: (d != mine, d))
    ]
