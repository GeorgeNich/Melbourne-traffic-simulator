# Phase 3 checkpoint — Melbourne Traffic Twin

2026-10-28 · FINAL. qa-reviewer: approve (round 1) and ratified (follow-up); both branches
merged to main (`8b84fd0` sim, `e9d54cb` web), `scripts/check.sh` and the full Playwright e2e
green (42 passed against the real export). Gate screenshots:
`docs/screenshots/phase-3/am-peak-0742.png` (07:42, "Real counts · Fri 23 Oct 2026 ·
calibrated 7–8 am" badge, 3,328 cars) and `baseline-after-switch.png`. Gate evidence:
`sim/out/checks/am_peak_calibration.json` (includes the original 85% two-hour gate recorded as
not met at 57.1%, and held-out 07–08 at 38.1% — nothing looks better than it measured).

Claude Code's mandate ends here: Phases 0–3 built, gate decided, everything after this is
George's (Phase 4: the AI brain) or deferred (Phases 5–6). This file is the handover.

## What was built

A browser 3D traffic twin of Melbourne CBD, Southbank and Docklands: ghost buildings (stacked
tiers, point-cloud-checked heights), 3D roads (bridges, the CityLink cutting, X-ray tunnels),
and replayed SUMO traffic coloured blue/amber/red by speed, on a dark OpenFreeMap base.
Phase 3 made the traffic real-ish: real SCATS counts (Fri 23 Oct 2026, 45 sites, 136
approaches), a repaired signal- and junction-layer built from the DTP configuration sheets,
and a demand fit calibrated to the counts.

## How to run it

```bash
uv sync                                   # Python + SUMO into .venv
. "$NVM_DIR/nvm.sh" && nvm use && pnpm install
pnpm -C web dev                           # app at http://localhost:5173
scripts/check.sh                          # every lint and test (all green at handover)
```

Rebuild the whole chain from raw data: `uv run python -m sim.build_net` (network),
`uv run python -m sim.signals_real` (real signal layer), `uv run python -m sim.am_peak all`
(calibration + exports). Scenario of record: **attempt 5** (`sim/work/am_peak/attempt5/`).

## Calibration numbers (the gate)

Gate as restated and accepted by George 2026-10-28 (PLAN decisions log): scored on the 07–08
calibrated hour with adaptive signal control, teleports and boundary backlog reported alongside;
the original 85%-both-hours gate is documented as not met, with the gap measured and attributed.

| Measure | Value |
| --- | --- |
| GEH < 5, calibration approaches, 07:00–08:00 | **75.4%** (86 of 114) |
| GEH < 5, 08:00–09:00 | 38.4% — published known limitation |
| Held-out sites (15%), gate windows | 31.0% |
| Teleports (06:30–09:30) | 4,504 (was 10,080 at the Phase 3 start) |
| Vehicles unable to insert by 09:30 | 7,554 |
| Replay window / file | 07:30–08:00, 9,622 vehicles, 67 MB (size accepted) |

Signal-regime evidence (fresh fits, same cars, same network): adaptive control 61.5% gate share
vs real uncoordinated programs 42.9% — the measured cost of SCATS coordination not being
published. The real programs stay in the model as a labelled layer (below), selectable but not
the gate.

## The real-signal layer (planner-tool data)

387 SUMO traffic lights: **49 run their genuine DTP phase plans**, 73 run real timings on
netconvert phases, 265 default. Every tier, reason and warning per light:
`sim/out/checks/signals_real.json`. Turn rules verified from the City of Melbourne sign-panel
open data (hook turn at Queen×Bourke; both King×Little Collins rights banned) and George's
spot-checked sheet readings.

Update loop proven (the Phase 5 planner-tool pattern): a real-world fact → one entry in a data
file with provenance (`sim/patches/net/local_knowledge_turns.con.xml`, the sheets JSON, 
`HOOK_TURN_SITES`) → rebuild → 4-minute verification run.

## Known issues (carried forward, each with numbers)

Carried forward from Phases 0–2, plus, from Phase 3:
- 08–09 saturation mismatch (38.4%): no trams/pedestrians/cyclists/buses, no SCATS
  coordination (unpublished), kerb parking counted as driving lanes.
- Demand under-determined: one Friday, 45 sites, no freeway/boundary counts; held-out 31%;
  7,554 vehicles cannot enter at the boundaries over the full 3 h.
- 14 lights with event-only turn groups (warned in signals_real.json); ~12 sites with
  unresolved sheet movements; 20 sites with no SUMO light within 60 m.
- William St corridor scores worse since the sheet-faithful lane arrows replaced
  misclassified ones (sites 2916/4583/4608 NB) — more real, fewer points.
- Flinders St WB right at King (2929 group 7): movement exists on the sheet, no network link.
- trips format: 67 MB for 30 min; the data-format change is deferred.
- **No en-route rerouting (George, watching the replay, 2026-10-28):** every vehicle follows its
  fitted route verbatim, so queues at committed bottlenecks (e.g. the Burnley tunnel inbound at
  ~07:40) run longer and denser than reality — no driver diverts to the visibly empty parallel
  route. Contributes to the 08–09 mismatch. SUMO's rerouting device was deliberately not
  enabled in Phase 3; trying it is a one-flag experiment for the next calibration.

## Notes for Phase 4 (George)

- Training data: `sim/work/am_peak/attempt*/report.json` show the fit/sim machinery; vary
  demand via `ATTEMPTS` entries. Edge stats are the supervised target shape.
- The measured adaptive-vs-real gap (61.5 vs 42.9) is a ready-made learning target: signal
  coordination/offsets are exactly what an RL or optimisation brain could tune, with the real
  phase plans as the constraint set.
- SCATS counts publish daily back to 2014 — the date-generic pipeline (PLAN decisions,
  2026-10-27) is the any-day training-set source.
- Trust the provenance fields everywhere (height_source, z_source, signal tiers, checklist
  evidence grades): they are the difference between training on measurements and training on
  guesses.
- **Car parks (George, 2026-10-28 — not yet used, data verified available):** two distinct
  improvements, both from City of Melbourne open data (CC BY):
  1. *Demand anchors:* "Off-street car parks with capacity and type" gives garage locations
     and space counts — weight internal trip ends by parking capacity instead of the current
     lanes×class guess. Likely helps the boundary-overload and held-out weakness directly.
  2. *Kerb capacity:* the parking sign-plate / Pay Stay zone datasets say which kerb lanes are
     parking (and when), fixing the "kerb parking counted as driving lanes" known issue with
     data rather than assumption. (Parking-sensor datasets exist too but are 2011–2016.)
  Neither entered Phase 3; add SOURCES.md entries before first use.
