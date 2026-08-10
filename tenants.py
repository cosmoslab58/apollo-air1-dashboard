"""Which home the dashboard is showing.

This container is *wired* to exactly one tenant. `MQTT_TOPIC_PREFIX` pins the
broker tree it subscribes to and publishes controls on, and the broker ACL
enforces that from the other side -- a prefix outside the grant is silently
denied at QoS 0 (see the README's broker section). So switching tenants here
switches what is **read back from InfluxDB**: indoor history and every outside
provider, all of which Node-RED polls in and tags with `tenant`. It does not,
and cannot, move live device state or the control endpoints; those stay pinned
to `own()`.

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
    """The tenant this container is wired to.

    First segment of the MQTT topic prefix -- the same convention Node-RED
    uses when it takes the tenant id off `+/air1/state`. Keeping one rule for
    "what is a tenant id" across both sides is what stops the dashboard and
    the ingestion flow from disagreeing about who a reading belongs to."""
    return os.environ.get("MQTT_TOPIC_PREFIX", "").split("/")[0]


def set_current(tenant):
    _current.set(tenant or None)


def current():
    """The tenant being viewed. Defaults to own() outside a request, so
    anything running off the request path (startup, background work) reads
    this container's own data rather than whatever was viewed last."""
    return _current.get() or own()


def own_device():
    """The device this container is wired to: second segment of the topic
    prefix, matching how the ingestion flow reads it off `<house>/<device>/state`.

    Note both halves moved together in the 2026-08-09 rename (cosmoslab/air1 ->
    walnut/aqm_alpha), which is exactly why neither is derived from the other."""
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
    """True when the viewed home AND device are the ones this container can
    actually talk to. The gate for live state, device controls, and config
    publishes.

    Device matters as much as home here: a command published to this
    container's prefix reaches ITS sensor, so viewing a different device in the
    same house must disable the controls just as firmly as viewing another
    house would. current_device() of None means "all devices", which is also
    not something a control can be aimed at."""
    if (tenant or current()) != own():
        return False
    mine = own_device()
    if not mine:
        # No device segment configured at all (no MQTT_TOPIC_PREFIX, or a
        # single-segment one). There is then no device dimension to disagree
        # about, so it must not gate anything -- otherwise every ownership
        # check fails closed and unrelated validation errors surface as 409s.
        return True
    return current_device() == mine


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
    (two homes may each run an `aqm_alpha`), so a global device list would
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
    """(id, label, is_own) for the device picker, own device first."""
    tenant = tenant or current()
    mine = own_device() if tenant == own() else None
    return [
        (device, label(device), device == mine)
        for device in sorted(known_devices(tenant), key=lambda d: (d != mine, d))
    ]
