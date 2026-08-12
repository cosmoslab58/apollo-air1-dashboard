"""The severity band table, as published by the AIR-1 itself.

There is exactly ONE definition of "how bad is a given reading" in this system,
and it does not live here. It lives in the firmware's `band_cuts_*` globals
(apollo-air1-mqtt.yaml), which grade the readings, drive the publish cadence,
and pick the LED color. The device publishes that table to a retained MQTT
topic; this module hands it to the browser so the page colors a CO2 tile with
the same numbers the light on the wall used.

Why the device owns it rather than this app:

- The AIR-1 has to color its LED with no network at all. It cannot depend on a
  dashboard for its own thresholds, so it must hold them regardless -- and a
  copy here would therefore be a *second* copy, not the only one.
- The numbers are health judgements with citations (CDC/ASHRAE/OSHA for CO2,
  EPA breakpoints for AQI). They belong next to that reasoning, which is in the
  firmware comments.

Consequence worth stating plainly: the page can no longer disagree with the
light -- and equally, that the page inherits whatever the firmware's tables say,
quirks included. That coupling is the feature: when the VOC green band turned out
to be drawn below the index's own resting value (making both the LED and this
dashboard read yellow about half the time), it was one edit in one place to fix
both, with no risk of fixing one and forgetting the other.
"""

import json
import logging
import os
import re
import threading

import mqtt_bridge

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
BANDS_FILE = os.path.join(DATA_DIR, "bands.json")

# Suffix under `<house>/<device>/` that the firmware publishes the table to,
# retained. mqtt_bridge already subscribes to the whole house tree, so nothing
# has to be wired up for this to arrive -- it lands in the bridge's state cache
# like any other topic.
BANDS_SUFFIX = "config/bands"

# The cache filename is built from a device id that arrives from a cookie or a
# query string, so it is matched against the same slug shape tenants.ID_RE
# enforces before it can become a path. Anything else gets no disk cache at all
# rather than a sanitised guess -- see _bands_file.
_DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_write_lock = threading.Lock()

# Channels the device grades. A payload missing any of them is treated as
# malformed rather than partially applied: a half-populated table would color
# some tiles from the device and leave others uncolored, which is exactly the
# split-brain this module exists to prevent.
REQUIRED_CHANNELS = ("aqi", "co2", "voc", "nox")


def _valid(table):
    """Structural check only -- we do not second-guess the device's numbers.

    The one thing worth rejecting is a payload we'd misread: non-numeric or
    non-ascending cuts would silently produce nonsense bands rather than an
    obvious failure.
    """
    if not isinstance(table, dict):
        return False
    for channel in REQUIRED_CHANNELS:
        cuts = table.get(channel)
        if not isinstance(cuts, list) or not cuts:
            return False
        if not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in cuts):
            return False
        # strict=False is the point here: cuts[1:] is deliberately one shorter,
        # this is a pairwise-neighbor walk.
        if any(b <= a for a, b in zip(cuts, cuts[1:], strict=False)):
            return False
    colors = table.get("colors")
    if not isinstance(colors, list) or not all(isinstance(c, str) for c in colors):
        return False
    return True


def _bands_file(device=None):
    """Where one device's last-known table is cached, or None for no cache.

    Per device now that one container serves a whole house: two sensors can be
    on different firmware, and serving the living room's cached cuts under the
    bedroom's readings would be precisely the invented-default this module
    exists to refuse. `None` keeps the historical shared path, which is what
    the deviceless callers (and these tests) use.

    An id that isn't a plain slug gets no cache rather than a sanitised
    filename: the value reaches the filesystem, and a hand-edited `?device=`
    must not be able to steer it."""
    if device is None:
        return BANDS_FILE
    if not _DEVICE_RE.match(device):
        return None
    return os.path.join(DATA_DIR, f"bands-{device}.json")


def _load_cached(device=None):
    path = _bands_file(device)
    if path is None or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            table = json.load(f)
    except (OSError, ValueError):
        logger.warning("bands: cached table at %s is unreadable, ignoring", path)
        return None
    return table if _valid(table) else None


def _save_cached(table, device=None):
    path = _bands_file(device)
    if path is None:
        return
    with _write_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(table, f, indent=2)
        os.replace(tmp, path)


def get_table(device=None):
    """One device's current band table, or None if this app has never seen one.

    Per device: the table is published by a specific sensor, so asking for it
    without naming one (the "all devices" view) has no answer -- /api/bands
    refuses that case rather than picking a sensor's table to speak for the
    house.

    Live MQTT wins; the disk cache is the fallback. That ordering matters after
    a firmware change: the retained topic carries the new table, and we must not
    keep serving a stale cached one just because it happens to be on disk.

    The disk cache exists for the window where the broker is unreachable at
    boot. Without it a broker outage would strip the color out of every page,
    which is a worse failure than showing thresholds that were correct the last
    time we heard from the device.

    None is a real answer, and callers must handle it: better an uncolored
    reading than a color this app invented, since inventing one is how the page
    and the LED drifted apart in the first place.
    """
    raw = mqtt_bridge.get_raw(device, BANDS_SUFFIX)
    if raw:
        try:
            table = json.loads(raw)
        except ValueError:
            logger.warning("bands: retained payload is not JSON, falling back to cache")
            table = None
        if table is not None:
            if _valid(table):
                if table != _load_cached(device):
                    try:
                        _save_cached(table, device)
                    except OSError:
                        logger.exception("bands: could not persist table (serving it anyway)")
                return table
            logger.warning("bands: retained payload failed validation, falling back to cache")
    return _load_cached(device)
