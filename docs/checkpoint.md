# Melbourne Traffic Simulator — technical checkpoint

This repository is a curated baseline release of a larger traffic-modelling project. It contains the application, prepared city geometry, simulation network, baseline vehicle replay, source code, and supporting reports. The larger morning-peak replay, source signal-sheet images, raw downloads, and internal development handovers are intentionally excluded.

## Included release

The browser visualises Melbourne's CBD, Southbank, and Docklands with a wider building context. It renders translucent stacked building pieces, bridges, freeway interchanges, the CityLink cutting, and X-ray tunnel traffic over a dark OpenFreeMap base.

| Baseline property | Recorded value |
| --- | --- |
| Traffic demand | Synthetic, moderate density (`placeholder_random`) |
| Replay duration | 900 seconds / 15 minutes |
| Distinct vehicles | 1,459 |
| Vehicles recorded on tunnel ways | 209 |
| Teleports | 0 |
| SUMO version | 1.28.0 |
| Seed | 42 |
| Rendered building pieces | 49,810 |
| SUMO road edges | 3,955 |

The on-screen building counter describes pieces, including stacked sections of individual structures. The road-edge count is recorded in `sim/out/checks/connectivity.json`; the application's separate elevated-road counter describes rendered ways and is not the same measure.

The baseline is not a measured off-peak period. Its real geography and synthetic demand are labelled separately. Only this scenario is bundled for playback.

## Run and verify

The browser replay needs Node 22, Corepack/pnpm, a WebGL-capable browser, and internet access for the basemap. From the repository root:

```bash
nvm use
corepack enable
pnpm install --frozen-lockfile
pnpm dev
```

Python and the raw downloads are not required to view the prepared replay. For the full local checks, install the Python dependencies with `uv sync`, then run `pnpm check`. Run `pnpm -C web e2e` for browser checks, including map attribution. Tests needing intentionally excluded source inputs skip when they are unavailable.

## Architecture and fixed interfaces

- `pipeline/` processes geospatial and transport data and records provenance and quality checks.
- `sim/` constructs the SUMO network, generates and evaluates scenarios, and exports vehicle paths and road-speed data.
- `web/` loads those outputs and presents the 3D city, traffic playback, speed overlays, and camera controls.
- `tests/contracts/` checks that the prepared data and exports agree across components.

The prepared files in `data/processed/`, the exports in `sim/out/`, and `sim/net/roads.net.xml` are fixed interfaces. Scenario trips and metadata form a pair; the synchronisation step rejects an incomplete pair. Display wording is independent of contract values such as `placeholder_random`.

The source-data register is [data/SOURCES.md](../data/SOURCES.md). Source drawings and raw downloads are not needed to run this public baseline release.

## Baseline quality measurements

Source: [trips_baseline_checks.json](../sim/out/checks/trips_baseline_checks.json).

| Check | Recorded result |
| --- | --- |
| Samples within a carriageway envelope | 99.684% — passes the 99% alignment gate |
| Samples within their own mapped way's envelope | 98.32% — reported separately |
| Playback chords more than 1.5 m outside the road surface | 0 of 226,648 |
| Maximum chord deviation from the road surface | 0.71 m |
| Maximum vertical step between exported points | 0.95 m |
| Unexpected ground samples inside building footprints | 4 of 234,054 — this diagnostic remains flagged |
| Samples driving along the checked car-free corridors | 0 |

### Building-footprint intersections

There are two flagged samples at each of two buildings: `c903326_4018` and `w989034007`. Together they account for approximately **0.0017%** of the checked ground samples. Slight OSM footprint/carriageway overlap or straight-line playback interpolation between recorded samples could make a turning vehicle clip a corner briefly. The cause is not conclusively established.

These samples have already been investigated and are retained as a known limitation. The checks JSON remains unchanged. Passing the automated suite does not imply that every stored geometry diagnostic passes.

### Height and source limitations

- The model uses a flat ground reference. Some sloping-ground and embankment relationships remain approximate.
- Tunnel depths are estimated. The Yarra clearance check assumes 6 m water depth, 5 m cover, and a 6 m tunnel structure; these are not measured bathymetry or surveyed tunnel geometry.
- The Domain Tunnel is drawn approximately 21.4–27.5 m below the model's ground beneath the Yarra. Published descriptions of a shallow cut-and-cover crossing do not independently validate those modelled depths.
- Post-2018 structures cannot be verified using the 2018 point cloud and retain estimated heights where required.
- A known report inconsistency records an inherited **6.06 m** cutting depth for the crossing of ways `26619046` and `5234953`, while the local drawn cutting depth is approximately **0.54 m**. The expected-failure test preserves this discrepancy; it is not an undisclosed passing check.
- Tunnel portal transitions and nearby open cuttings can produce visual changes in X-ray classification and glow width.

## Wider calibration experiments — historical results

The repository retains calibration code and reports from experiments on **Friday 23 October 2026**, using real aggregated SCATS counts at 45 sites. The final calibration report uses **135 approaches**, of which 114 were calibration approaches and 21 were held out. The associated vehicle replay and its metadata and speed export are not distributed in this release.

Source: [am_peak_calibration.json](../sim/out/checks/am_peak_calibration.json). These results describe that historical experiment, not the bundled synthetic baseline.

| Measure | Recorded result |
| --- | --- |
| Calibration approaches with GEH < 5, 07:00–08:00 | 86 / 114 = 75.4% |
| Calibration approaches with GEH < 5, 08:00–09:00 | 43 / 112 = 38.4% |
| Original two-hour calibration gate | 129 / 226 = 57.1%; original 85% target not met |
| Held-out approaches with GEH < 5, 07:00–08:00 | 8 / 21 = 38.1% |
| Held-out approach-hours with GEH < 5, both hours | 13 / 42 = 31.0% |
| Teleports, 06:30–09:30 | 4,504 |
| Vehicles waiting to enter at 09:30 | 7,554 |
| Simulated collisions recorded over the run | 130 |

GEH is a comparison of observed and modelled flows. The share satisfying a GEH threshold is not a general percentage-accuracy score.

Earlier comparative runs recorded a two-hour gate share of **61.5% with adaptive control** and **42.9% with uncoordinated real signal programs**. Published signal plans do not supply live SCATS coordination; these comparisons quantify a model gap rather than establishing a single cause for it.

The available real-signal reference layer contains **49 lights with real phase plans**, **73 with real timings on generated phases**, and **265 using defaults**, out of 387. This layer is retained in `sim/net/signals_real.add.xml`, with tiers and reasons in [signals_real.json](../sim/out/checks/signals_real.json). Its availability does not mean that those programs are active in the bundled baseline replay.

### Remaining modelling limits

- Demand is underdetermined by one day's counts at a limited set of sites, with no fitted freeway or boundary counts; held-out performance remains weak.
- Trams, buses, pedestrians, and cyclists are not simulated. Missing interactions, kerb parking treated as driving capacity, and unavailable SCATS coordination constrain peak-period results.
- Vehicles retain their fitted routes; no en-route rerouting was enabled in the historical calibration. Queues at committed bottlenecks can coexist with unused parallel routes.
- The signal report identifies 14 lights with event-only turn groups, approximately 12 unresolved sheet movements, and 20 sites without a matched SUMO light within 60 m.
- The William Street corridor scores worse after sheet-faithful lane-arrow corrections. The Flinders Street westbound right turn at King Street appears in the source movement data but has no corresponding network link.

Future work can examine lane fidelity, parking and employment-based trip-end weights, route adaptation, and signal coordination. These are research directions, not implemented planning controls in this release.

## Images and licensing

The README's animated preview is captured from this public baseline application.

Source code is available for viewing and evaluation under [LICENSE.md](../LICENSE.md), with all rights reserved. OpenStreetMap-derived datasets retain ODbL licensing, and other data retains its source licences and attribution as recorded in [data/SOURCES.md](../data/SOURCES.md). The code licence does not relicense the datasets. OpenStreetMap, OpenFreeMap, OpenMapTiles, and City of Melbourne credits remain visible on the map.
