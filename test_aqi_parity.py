"""Guards that the browser's EPA AQI math (static/aqi.js) stays in lockstep
with the Python source of truth (epa_aqi.py). A breakpoint revision has to be
made in both languages; this fails loudly if only one is updated.

- test_breakpoint_tables_match: pure-Python, always runs -- compares the
  AQI_BREAKPOINTS table parsed out of aqi.js against epa_aqi.BREAKPOINTS.
- test_functional_parity_with_node: runs aqi.js under node (skipped if node
  isn't installed) and checks aqiFromConcentration matches epa_aqi across a
  grid of parameters/units, so the interpolation + unit handling agree too.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import epa_aqi

_AQI_JS = os.path.join(os.path.dirname(__file__), "static", "aqi.js")


def _parse_js_breakpoints():
    text = open(_AQI_JS, encoding="utf-8").read()
    match = re.search(r"const AQI_BREAKPOINTS = (\{.*?\n\});", text, re.DOTALL)
    assert match, "AQI_BREAKPOINTS object not found in static/aqi.js"
    return json.loads(match.group(1))


def test_breakpoint_tables_match():
    js = _parse_js_breakpoints()
    py = {param: [list(row) for row in rows] for param, rows in epa_aqi.BREAKPOINTS.items()}
    assert js == py, "static/aqi.js AQI_BREAKPOINTS drifted from epa_aqi.BREAKPOINTS"


def _py_aqi(parameter, value, units):
    """The Python side of the comparison: the shared unit normalizer plus the
    shared interpolation, so the two languages are compared on identical
    inputs. Deliberately calls epa_aqi.epa_value rather than re-deriving the
    unit handling here -- a local copy would keep passing while production
    code skipped the conversion (which is exactly how the Away dominant-
    pollutant CO bug survived this test)."""
    return epa_aqi.aqi_from_concentration(parameter, epa_aqi.epa_value(parameter, value, units))


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_functional_parity_with_node():
    cases = []
    for parameter in ("PM2.5", "PM10", "O3", "NO2", "SO2", "CO"):
        for value in (0.5, 5, 12, 40, 90, 150, 300):
            for units in ("PARTS_PER_BILLION", "MICROGRAMS_PER_CUBIC_METER"):
                cases.append([parameter, value, units])

    runner = (
        open(_AQI_JS, encoding="utf-8").read()
        + "\nconst cases = " + json.dumps(cases) + ";\n"
        + "console.log(JSON.stringify(cases.map(c => aqiFromConcentration(c[0], c[1], c[2]))));\n"
    )
    out = subprocess.run(["node", "-e", runner], capture_output=True, text=True, check=True)
    js_results = json.loads(out.stdout)

    for (parameter, value, units), js in zip(cases, js_results, strict=True):
        assert js == _py_aqi(parameter, value, units), f"parity mismatch for {parameter} {value} {units}: js={js}"
