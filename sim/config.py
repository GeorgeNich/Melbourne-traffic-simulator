"""Paths and fixed settings for the SUMO side. Bboxes live in pipeline/config.py, never here."""

from __future__ import annotations

import os
from pathlib import Path

import sumo

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = REPO_ROOT / "sim"

OSM_EXTRACT = REPO_ROOT / "data" / "processed" / "osm_sim_area.osm.gz"

NET_DIR = SIM_DIR / "net"
NET_PATH = NET_DIR / "roads.net.xml"
# Which OSM nodes netconvert merged into each joined junction (maps OSM nodes to junctions).
JOINED_PATH = NET_DIR / "joined_junctions.xml"

PATCH_DIR = SIM_DIR / "patches"
OSM_PATCH_DIR = PATCH_DIR / "osm"  # applied while importing OSM (types, node joins)
NET_PATCH_DIR = PATCH_DIR / "net"  # applied to the imported network (connections, TLS, edges)

WORK_DIR = SIM_DIR / "work"  # gitignored intermediates
OUT_DIR = SIM_DIR / "out"
CHECKS_DIR = OUT_DIR / "checks"

SUMO_HOME = Path(sumo.SUMO_HOME)
SUMO_TOOLS = SUMO_HOME / "tools"
SUMO_TYPEMAP = SUMO_HOME / "data" / "typemap" / "osmNetconvert.typ.xml"

# One seed for everything stochastic on the SUMO side (netconvert, randomTrips, sumo).
SEED = 42

# The junction-join distance (m). See sim/build_net.py for why 15 m.
JUNCTION_JOIN_DIST_M = 15.0


def sumo_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("SUMO_HOME", str(SUMO_HOME))
    return env
