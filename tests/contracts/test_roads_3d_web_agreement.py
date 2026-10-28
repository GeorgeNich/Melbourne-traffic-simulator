"""The web roads_3d validator (web/src/data/roadsContract.ts) agrees with the Python contract.

Written by qa-reviewer. Reads the TypeScript source as text (no Node needed) and checks that the
property names and types it validates are exactly the ones in test_roads_3d_contract.py, so the two
can't drift apart silently. Skips when the web validator doesn't exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.contracts.test_roads_3d_contract import EXPECTED_KEYS, Z_SOURCES

ROOT = Path(__file__).resolve().parents[2]
TS = ROOT / "web" / "src" / "data" / "roadsContract.ts"

pytestmark = pytest.mark.skipif(not TS.exists(), reason="web roads validator not written yet")

# Python contract types -> the TypeScript type the web interface must declare.
EXPECTED_TS_TYPES = {
    "way_id": "number",
    "highway": "string",
    "layer": "number",
    "bridge": "boolean",
    "tunnel": "boolean",
    "name": "string | null",
    "z_source": "ZSource",
}


@pytest.fixture(scope="module")
def source() -> str:
    return TS.read_text(encoding="utf-8")


def test_validated_property_list_matches_contract(source: str):
    m = re.search(r"ROAD_PROPERTIES\s*=\s*\[([^\]]*)\]", source)
    assert m, "ROAD_PROPERTIES not found in roadsContract.ts"
    names = re.findall(r'"([^"]+)"', m.group(1))
    assert len(names) == len(set(names))
    assert set(names) == EXPECTED_KEYS


def test_interface_types_match_contract(source: str):
    m = re.search(r"interface RoadProperties\s*\{([^}]*)\}", source)
    assert m, "RoadProperties interface not found in roadsContract.ts"
    fields = dict(re.findall(r"(\w+)\s*:\s*([^;]+);", m.group(1)))
    assert {k: v.strip() for k, v in fields.items()} == EXPECTED_TS_TYPES


def test_z_source_values_match_contract(source: str):
    m = re.search(r"Z_SOURCES\s*=\s*\[([^\]]*)\]", source)
    assert m, "Z_SOURCES not found in roadsContract.ts"
    names = re.findall(r'"([^"]+)"', m.group(1))
    assert len(names) == len(set(names))
    assert set(names) == Z_SOURCES
    assert re.search(r"type ZSource\s*=\s*\(typeof Z_SOURCES\)\[number\]", source)
    assert "Z_SOURCES" in source.split("ROAD_PROPERTIES", 1)[1], "the validator must check z_source"


def test_positions_are_lon_lat_z(source: str):
    assert re.search(r"type Position3\s*=\s*\[number,\s*number,\s*number\]", source)
    assert "p.length !== 3" in source, "the web must reject 2D positions, as the contract does"
