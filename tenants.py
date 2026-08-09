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
# A year: the picker is a "whose house am I looking at" preference, not
# session state, and a wall display should survive a reboot still showing the
# home it was set to.
COOKIE_MAX_AGE_S = 365 * 24 * 3600

_current = contextvars.ContextVar("current_tenant", default=None)

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


def is_own(tenant=None):
    """True when the viewed tenant is the one this container can actually
    talk to. The gate for live state, device controls, and config publishes."""
    return (tenant or current()) == own()


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
