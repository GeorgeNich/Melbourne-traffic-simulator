"""Repo hygiene checks that protect parallel agent worktrees.

Agent worktrees live under `.claude/worktrees/` inside the main checkout. If that folder is not
git-ignored, the main checkout's `ruff check .` / `ruff format --check .` (scripts/check.sh and the
pre-commit hook) lint every agent's work in progress, and `git add -A` stages each worktree as an
embedded repository (gitlink).
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=ROOT, capture_output=True, check=False
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [".claude/worktrees/some-agent/pipeline/wip.py", ".claude/worktrees/some-agent/.git"],
)
def test_agent_worktrees_are_ignored(path: str):
    assert _is_ignored(path), f"{path} is not git-ignored; add `.claude/worktrees/` to .gitignore"


@pytest.mark.parametrize(
    "path",
    [
        "data/processed/osm_sim_area.osm.gz",
        "data/processed/roads_3d.geojson",
        "sim/net/roads.net.xml",
        "data/processed/buildings.geojson",
        "data/processed/counts.parquet",
        "sim/out/trips_baseline.json",
        "sim/out/baseline.meta.json",
        "sim/out/edge_stats_am_peak.parquet",
    ],
)
def test_contract_outputs_are_not_ignored(path: str):
    # Contract outputs are committed so other worktrees can see them (PLAN.md Decisions log).
    assert not _is_ignored(path)
