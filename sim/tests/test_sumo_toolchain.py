"""Phase 0 smoke test: proves the SUMO toolchain works end to end.

Uses a synthetic 3x3 grid from netgenerate. This checks the tools, not any real data.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sumo
import sumolib
import traci

os.environ.setdefault("SUMO_HOME", sumo.SUMO_HOME)
TOOLS = Path(sumo.SUMO_HOME) / "tools"


def test_sumo_version():
    out = subprocess.run(
        [sumolib.checkBinary("sumo"), "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert "Eclipse SUMO sumo 1.28.0" in out


def test_libsumo_imports():
    import libsumo

    assert hasattr(libsumo, "simulation")


def test_bundled_tools_present():
    assert (TOOLS / "randomTrips.py").is_file()
    assert (TOOLS / "routeSampler.py").is_file()


@pytest.fixture
def grid_scenario(tmp_path: Path) -> tuple[Path, Path]:
    net = tmp_path / "grid.net.xml"
    routes = tmp_path / "grid.rou.xml"
    subprocess.run(
        [
            sumolib.checkBinary("netgenerate"),
            "--grid",
            "--grid.number",
            "3",
            "--grid.length",
            "200",
            "--default.speed",
            "13.89",
            "-o",
            str(net),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(TOOLS / "randomTrips.py"),
            "-n",
            str(net),
            "-r",
            str(routes),
            "-o",
            str(tmp_path / "grid.trips.xml"),
            "-e",
            "60",
            "-p",
            "2",
            "--seed",
            "42",
        ],
        check=True,
        capture_output=True,
    )
    return net, routes


def test_traci_runs_and_vehicles_move(grid_scenario: tuple[Path, Path]):
    net, routes = grid_scenario
    traci.start(
        [
            sumolib.checkBinary("sumo"),
            "-n",
            str(net),
            "-r",
            str(routes),
            "--seed",
            "42",
            "--no-step-log",
            "--no-warnings",
        ]
    )
    try:
        departed, max_speed = 0, 0.0
        for _ in range(60):
            traci.simulationStep()
            departed += traci.simulation.getDepartedNumber()
            for vid in traci.vehicle.getIDList():
                max_speed = max(max_speed, traci.vehicle.getSpeed(vid))
        assert traci.simulation.getTime() == pytest.approx(60.0)
    finally:
        traci.close()
    assert departed > 0
    assert max_speed > 1.0
