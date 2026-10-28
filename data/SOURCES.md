# Data sources

Every dataset used in this project, added *before* it is used. Open licences only (CLAUDE.md, hard constraint 3).
Aggregated counts only; no personal data, no camera or image data.

| Dataset | Publisher | URL | Licence | Attribution required | Downloaded | Used for | Raw file(s) in `data/raw/` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenStreetMap regional extract "Melbourne" (BBBike, bbox 144.68,-38.02 → 145.30,-37.53, OSM data as of 2026-10-24), clipped locally to `BUILDINGS_BBOX` / `SIM_BBOX`. Route used because the single Overpass bbox query (query text in `pipeline/download.py`) was refused with HTTP 502 on 2026-10-26 and was not retried | OpenStreetMap contributors (OpenStreetMap Foundation); extract by BBBike.org | https://download.bbbike.org/osm/bbbike/Melbourne/Melbourne.osm.pbf (licence: https://www.openstreetmap.org/copyright) | ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/) | Yes: "© OpenStreetMap contributors" | 2026-10-26 | Building footprints, `height` and `building:levels` for `buildings.geojson` (Phase 1); drivable roads, lanes, signals, turn restrictions and `bridge`/`tunnel`/`layer` tags for `osm_sim_area.osm.gz` and `roads_3d.geojson` (Phase 2; the SUMO network `sim/net/roads.net.xml` is built from the former) | `bbbike_melbourne.osm.pbf` (or `osm_buildings_bbox.osm` if a future Overpass query succeeds) |
| 2023 Building Footprints (dataset id `2023-building-footprints`) | City of Melbourne (data.melbourne.vic.gov.au) | https://data.melbourne.vic.gov.au/explore/dataset/2023-building-footprints/ | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/legalcode, as stated on the dataset page) | Yes. Exact text for the map: "Building heights: Contains City of Melbourne Open Data (CC BY 4.0), modified", linking to https://creativecommons.org/licenses/by/4.0/ | 2026-10-26 | Building height fallback (`footprint_max_elevation − structure_min_elevation`, metres) for OSM buildings with no `height` or `building:levels`; covers the City of Melbourne municipality only. Despite the dataset name, footprints carry older capture dates (`date_captured`: 32,792 from 2018-05-28, 4,482 from 2022-01-20, 4,264 from 2023-05-11, 163 from 2020-05-15), so these heights are not "2023 measurements" | `com_2023_building_footprints.geojson` |
| Vicmap Transport: Road Line (`tr_road`) and Road Infrastructure Point (`tr_road_infrastructure`), requested once each from the public Vicmap open-data WFS for `SIM_EXTRACT_BBOX` only (GeoJSON, EPSG:4326; the request URLs are in `pipeline/download.py`). No form, account or key | Department of Transport and Planning, State of Victoria (Vicmap, Land Use Victoria) | https://discover.data.vic.gov.au/dataset/vicmap-transport-road-line and https://discover.data.vic.gov.au/dataset/vicmap-transport-road-infrastructure-point (WFS: https://opendata.maps.vic.gov.au/geoserver/wfs; metadata: https://metashare.maps.vic.gov.au/geonetwork/srv/api/records/61f633ae-c18c-5967-a546-84ceb44273f6) | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/, as stated on both dataset pages and in the metadata record) | Yes: "© State of Victoria (Department of Transport and Planning), Vicmap Transport, CC BY 4.0" | 2026-10-26 | **Verification only** (Phase 2 road network check): an independent cross-check of OSM bridges, tunnels and junctions (`pipeline/checks/vicmap_crosscheck.py`). Not used to build any contract file | `vicmap_tr_road.geojson`, `vicmap_tr_road_infrastructure.geojson` |
| City of Melbourne 3D Point Cloud 2018 (dataset id `city-of-melbourne-3d-point-cloud-2018`): photogrammetric point cloud (.las tiles, MGA Zone 55 / EPSG:28355, heights in AHD, RGB per point), captured May 2018 at 7.5 cm ground sample distance, 25 cm stated absolute XYZ accuracy. **Photogrammetry, used under George's exception** (CLAUDE.md constraint 1, PLAN.md Decisions log 2026-10-26): we read published heights as ordinary data and run no computer vision. Only the tiles whose footprint (from the published `Tile_Index.KML`) intersects `BUILDINGS_BBOX` (which contains `SIM_EXTRACT_BBOX`) are fetched, in one pass, as single HTTP range requests out of the published zip (no form, account or key). 2018 capture: structures built later (e.g. West Gate Tunnel Project viaducts) are absent | City of Melbourne (data.melbourne.vic.gov.au) | https://data.melbourne.vic.gov.au/explore/dataset/city-of-melbourne-3d-point-cloud-2018/ (zip: https://opendatasoft-s3.s3.ap-southeast-2.amazonaws.com/attachments/CoM_Point_Cloud_2018_LAS.zip, 4,333,040,949 bytes, ETag c669195ef0be6cc023a3eb34814e258a; tile index: https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/city-of-melbourne-3d-point-cloud-2018/attachments/tile_index_kml) | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/legalcode, as stated by the publisher's portal; an older data.gov.au harvest says CC BY 3.0 AU, the publisher's own page is taken as authoritative) | Yes: "Road deck heights: Contains City of Melbourne Open Data (3D Point Cloud 2018, CC BY 4.0), modified", linking to https://creativecommons.org/licenses/by/4.0/ | 2026-10-26 | Measured road deck heights for bridge ways in `roads_3d.geojson` (deck surface minus nearby ground, `z_source: measured`), only where the sample is dense and consistent; Phase 2 Stage B: the depth profile of the CityLink cutting under Power St (street level minus the open-to-the-sky road surface, `z_source: measured`). Never used for tunnels (it cannot see underground or under water). Building heights from the same tiles are a separate, later task; not used for buildings yet | `com_pointcloud_2018/Tile_Index.KML`, `com_pointcloud_2018/<tile>.las` (tiles over `BUILDINGS_BBOX`; only those over `SIM_EXTRACT_BBOX` are read for road decks), `com_pointcloud_2018/MANIFEST.json` (tile names, byte ranges, sizes, sha256) |

## Derived outputs

**Map credit (the single combined line shown on the map for all City of Melbourne heights):**
"Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified", linking to
https://creativecommons.org/licenses/by/4.0/ (orchestrator decision 2026-10-26; it covers the 2023
Building Footprints and the 3D Point Cloud 2018 rows above, whose own attribution columns keep the
per-dataset detail), together with "© OpenStreetMap contributors".

- `data/processed/buildings.geojson` is a **derived database of OpenStreetMap data under ODbL 1.0**
  (© OpenStreetMap contributors), with heights partly from City of Melbourne Open Data (CC BY 4.0,
  modified). Any public use must keep both attributions: "© OpenStreetMap contributors" and
  "Building heights: Contains City of Melbourne Open Data (CC BY 4.0), modified"
  (https://creativecommons.org/licenses/by/4.0/).
- `data/processed/osm_sim_area.osm.gz` (built by `pipeline/roads.py`) is a **derived database of
  OpenStreetMap data under ODbL 1.0** (© OpenStreetMap contributors): drivable `highway` ways clipped
  to `SIM_BBOX` + `SIM_BUFFER_M` (300 m), their nodes (incl. `highway=traffic_signals`) and
  `type=restriction` relations, with tags reduced to the keys listed in `pipeline/roads.py`
  (Stage C request: `bus:lanes*`, `lanes:bus*`, `busway` (+ `:left` / `:right` / `:both`) and
  `maxspeed:conditional` are kept exactly as tagged). OSM user names, uids, changesets and
  timestamps are not written. **Extended tunnel clip** (Phase 2 Stage B, orchestrator 2026-10-27):
  CityLink motorway tunnel ways (`tunnel=yes`, `highway=motorway` / `motorway_link`) are clipped at
  `TUNNEL_EXTRACT_BBOX` (east edge = `BUILDINGS_BBOX` east, 144.990) instead of `SIM_EXTRACT_BBOX`,
  so the Burnley (way 8066124) and Domain (way 8066126) tunnels include their Yarra crossings; this
  adds 16 nodes to those 2 existing ways and no new ways. The file's `<bounds>` stay
  `SIM_EXTRACT_BBOX`. `roads_3d.geojson` follows the same clip.
- `data/processed/roads_3d.geojson` (built by `pipeline/roads_3d.py` from the extract above, with
  context from the BBBike extract and the City of Melbourne 3D Point Cloud 2018) is a **derived
  database of OpenStreetMap data under ODbL 1.0** (© OpenStreetMap contributors) **with road deck
  heights partly from City of Melbourne Open Data (CC BY 4.0, modified)**. Any public use must keep
  both attributions: "© OpenStreetMap contributors" and the combined map credit above. Heights are
  **visual, relative to street level, never used by SUMO**. OSM `layer` (with `bridge`/`tunnel`) gives only
  the order at a crossing; each upper deck sits a separation above what is directly beneath it
  (road or railway). A point-cloud deck-to-road separation is an **anchor** (the deck sits exactly
  there); over a freeway in a cutting it is measured against street level, and the crossing is
  listed in `checks/deck_pointcloud.json` → `trench_crossings` with the cutting's depth (Stage B
  lowers the cutting; see below).
  Every road bridge (never a tunnel) gets a point-cloud deck profile measured against street
  level: its at-grade approach roads beyond the embankment, else nearby at-grade streets
  (widening to 1 km over water, e.g. the Bolte Bridge), never a cutting floor or water. Otherwise
  the lower road's OSM `maxheight` clearance + a deck thickness (a lower bound only, never an
  anchor), else a calibrated separation; unmeasured river bridges take the median of the
  measured street river bridges; measured service / car-park ramps may be up to 12 % steep.
  A point-cloud measurement that contradicts its neighbours within the grade limits is kept as a
  lower bound at its measured value, never replaced by `maxheight`. Separations and deck thickness are **calibrated from the point cloud in every
  build** (medians of the measured values, recorded with their n in `checks/deck_pointcloud.json`
  → `calibration`): first the deck's own measured median, then the median for its road class
  (freeway: motorway, trunk and links; other), then the global median; class/global medians need
  n >= 5 (fixed fallbacks 6.5 m / 1.2 m otherwise). **Measured always wins**: estimates are
  capped below measured decks and bent (to no less than 4.03 m over their own road), never the
  reverse; point-cloud measurements that contradict each other within the grade limits (terrain
  slope against a flat model ground) are dropped with that reason, and the few spots where kept
  measurements still leave less than 4 m are listed (`precedence`). River bridges (over or within
  60 m of water on the same deck) keep their profile or 1.5 m; an unmeasured freeway-class bridge
  with nothing beneath gets a floor of layer x the freeway separation (post-2018 viaducts such as
  the West Gate Tunnel decks), also capped by measurements (`layer_floors`); decks do not
  sag between crossings; ramps fall at <= 6 % (8 % only on short links where a crossing needs it);
  everything else is 0 (the least heights meeting all rules). Property **`z_source`** labels each
  way: `measured` (at least half of its raised vertices are held by point-cloud measurements or
  OSM `maxheight` clearances; which ones are in `checks/deck_pointcloud.json` → `anchors` / the
  OSM tags), `estimated` (raised
  by the rules only), `ground` (all z = 0). The point cloud is a 2018 capture: structures built
  later (West Gate Tunnel Project viaducts) are detected as missing and fall back to the rules.
  **Below street level (Phase 2 Stage B, all estimated except the measured cutting):**
  - Tunnels (`tunnel=*` except `building_passage` / `covered`, layer <= 0 or untagged): target
    depth -10 m x max(-layer, 1) (-10 / -20 / -30 m for CityLink layers -1 / -2 / -3), ramped
    down from the portals at the same grade limits (6 %, 8 % short links; steps <= 1 m; one z
    per shared node), so short tunnels only get as deep as their portals allow and link tunnels
    come down to meet the deep tunnel they join. Building passages and positive-layer tunnels
    (Kings Way 12278361 / 23212187) keep their Stage A heights. Labelled `estimated`.
  - A road (or railway) over a tunnel stays at least the tunnel's numeric `maxheight` + 1.2 m, else
    6.5 m, above it; where a portal is too close for that inside the tunnel, the approach road
    outside the portal ramps down as far as needed (`estimated`; listed in
    `checks/deck_pointcloud.json` → `below_ground.approach_ways_lowered`).
  - **Tunnels under water** (the Yarra, river-type water areas from the BBBike extract incl. the
    multipolygon relation 954522): the road surface stays at or below water surface - river depth
    - cover - tunnel roof = 0 - 6 - 5 - 6 = **-17 m**. All four values are **assumptions, not
    measurements**: the water surface is taken at the model's flat street level (the real Yarra
    surface is lower than its banks, so the real bed is deeper still); 6 m river depth is the upper
    end of George's ~5-6 m figure (no open bathymetric survey of the city reach was found); 5 m of
    cover and a 6 m road-to-roof tunnel structure are assumed. Ponds and fountains are not rivers:
    a tunnel under one keeps its normal depth (listed as `under_shallow_water`). The -10 m x level
    rule already puts the Burnley and Domain tunnels below this limit; plausibility check against
    published figures (facts only, see References): the Burnley Tunnel reaches 65 m at its deepest
    point (the Swan St shaft, east of our clip), the Domain Tunnel's road level is at most 40 m deep
    and it crosses the Yarra in a shallow cut-and-cover section. Our -30 m is within both.
  - The **CityLink cutting under Power St** (ways 5234624, 5234646, 5234953, 5234957, 19792505,
    980110339, 1175874270) is lowered to its depth **measured** in the City of Melbourne 3D Point
    Cloud 2018 (street level minus the road surface at each 10 m vertex; hidden stretches under the
    bridges filled by interpolation or by the depth measured at the crossing), ramped to 0 at the
    cutting ends; `z_source: measured` where at least half its lowered vertices hold their
    measurement. Measured vertices the 6 % limit cannot hold are drawn as close as it allows
    (`below_ground.pins_demoted`, <= 0.36 m off). A deck separation measured to the cutting floor
    is drawn as measured (the deck stands that much minus the cutting depth above street level).
  - `z_source` keeps its three values: `ground` = every z is 0; tunnels and lowered approach roads
    are `estimated`.
- `data/processed/checks/` holds **verification reports, not contract files** (Phase 2 road network
  verification; they change nothing in the contract files):
  - `grade_separation.json` and `grade_separation_flags.geojson` (built by
    `pipeline/checks/grade_separation.py` from `osm_sim_area.osm.gz` and `roads_3d.geojson`, with
    context from the BBBike extract): every crossing of two drivable ways without a shared OSM node,
    classified by layer vs `roads_3d` z, plus every bridge end and mid-deck junction. Derived from
    OpenStreetMap data, ODbL 1.0 (© OpenStreetMap contributors).
  - `vicmap_crosscheck.json` and `vicmap_crosscheck_flags.geojson` (built by
    `pipeline/checks/vicmap_crosscheck.py`): the OSM model compared with Vicmap Transport (bridges,
    tunnels, which road is on top at each crossing, junction vs structure). Contains Vicmap road names,
    PFIs/UFIs and structure types, so it is derived from both OpenStreetMap (ODbL 1.0, © OpenStreetMap
    contributors) and Vicmap Transport (© State of Victoria, Department of Transport and Planning,
    CC BY 4.0). Vicmap data is used for verification only and never enters a contract file; its lines
    are clipped to `SIM_EXTRACT_BBOX` before comparison.
  - `deck_pointcloud.json` (written by `pipeline/roads_3d.py` via `pipeline/deck_pointcloud.py`):
    every point-cloud measurement tried (road-over-road crossings and open decks), with point
    counts, spread (IQR), the separation or profile, whether it was used and why not; Stage B:
    `cutting_profiles` (the measured cutting depths), `trench_crossings` (every crossing over the
    cutting: measured trench depth, the cutting height drawn, final clearance),
    `pending_trench_depth` (those still under 4 m: none) and `below_ground` (tunnel depths, crossings
    over tunnels, the river-bed assumptions, water areas above tunnels, lowered approach roads,
    demoted cutting measurements). Derived from OpenStreetMap (ODbL 1.0) and City of Melbourne Open
    Data (3D Point Cloud 2018, CC BY 4.0).
  - Each report records `inputs_sha256` (the sha256 of every input file it read), so a report that no
    longer describes the current `roads_3d.geojson` / `osm_sim_area.osm.gz` is detectable.

## References (facts cited, no data downloaded)

Published figures used only to check that estimated heights are plausible; nothing is copied into a
contract file.

| Reference | URL | Licence | Read | Used for |
| --- | --- | --- | --- | --- |
| Wikipedia, "Burnley Tunnel" | https://en.wikipedia.org/wiki/Burnley_Tunnel | CC BY-SA 4.0 (text); single facts only | 2026-10-27 | Plausibility of the Stage B tunnel depth: 3.4 km long, "passes deep under the Yarra River", maximum depth 65 m, 6.2 % downhill at the west end |
| Wikipedia, "Domain Tunnel" | https://en.wikipedia.org/wiki/Domain_Tunnel | CC BY-SA 4.0 (text); single facts only | 2026-10-27 | Plausibility of the Stage B tunnel depth: 1.6 km long, road level at most 40 m deep, a shallow cut-and-cover river crossing, gradient under 2 % |
| City of Melbourne, "Bus lanes" | https://www.melbourne.vic.gov.au/bus-lanes | © City of Melbourne (not open-licensed); operating hours used as single cited facts only, approved by George 2026-10-27 | 2026-10-27 | Bus-lane hours in the sim: Queen St east side Mo–Fr 16:30–18:30 (Flinders Ln–Lonsdale St); Lonsdale St north side Mo–Fr 16:00–18:30 (Spencer–Exhibition); Lonsdale St south side Mo–Fr 07:00–09:30 (Spencer–Spring); City Rd inbound Mo–Fr 06:30–10:00 (Clarendon St–Queens Bridge St). Spring St listed without hours. Bus lanes not on this page stay always-on (known issue) |

## Phase 2: stacked-tier building heights, whole City of Melbourne (data-engineer, 2026-10-27)

Added in its own section so it merges cleanly with the Stage B edits above. These rows extend the
table at the top; where they differ from it for buildings, this section wins.

| Dataset | Publisher | URL | Licence | Attribution required | Downloaded | Used for | Raw file(s) in `data/raw/` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Municipal boundary (dataset id `municipal-boundary`): one polygon, the boundary of the City of Melbourne municipality (portal `modified` 2023-02-26, `data_processed` 2022-11-28). One GeoJSON export request, no form, account or key | City of Melbourne (data.melbourne.vic.gov.au) | https://data.melbourne.vic.gov.au/explore/dataset/municipal-boundary/ (export: https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/municipal-boundary/exports/geojson) | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/legalcode, as stated in the dataset's portal metadata) | Yes: covered by the combined map credit "Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified" | 2026-10-27 | The building extent `BUILDINGS_AREA` = this boundary ∪ `BUILDINGS_BBOX` (`pipeline/config.py`): which buildings are emitted, which use City of Melbourne stacked tiers, and which point-cloud tiles are fetched. Render only; the simulated area is unchanged | `com_municipal_boundary.geojson` |
| City of Melbourne 3D Point Cloud 2018 (same dataset, licence and download mechanism as the row above): **extension for buildings**. The remaining tiles whose footprint (published `Tile_Index.KML`) intersects `BUILDINGS_AREA` are fetched in one more pass (`--pointcloud-plan`, then `--pointcloud-only`), one HTTP range request per tile, no retries | City of Melbourne (data.melbourne.vic.gov.au) | as above | CC BY 4.0 | Yes: covered by the combined map credit | 2026-10-27 | **Building heights** in `buildings.geojson`: a robust roof statistic per City of Melbourne tier, used to confirm (or flag) the tier's height (`height_source: com_pointcloud_2018`); see the buildings bullet below | `com_pointcloud_2018/<tile>.las` (now the tiles over `BUILDINGS_AREA`), `com_pointcloud_2018/MANIFEST.json` |
| 2023 Building Footprints (same dataset as the row above): **now the primary building geometry inside the City of Melbourne**. Each `Structure` footprint is one stacked tier of a structure (`footprint_min_elevation` → `footprint_max_elevation`, metres; `structure_min_elevation` = the structure's ground) | City of Melbourne | as above | CC BY 4.0 | Yes: covered by the combined map credit | 2026-10-26 (no new download) | Building pieces (tiers) and their `height_m` / `min_height_m` in `buildings.geojson` inside the municipality | `com_2023_building_footprints.geojson` |

**Derived outputs (this section supersedes the `buildings.geojson` bullet above):**

- `data/processed/buildings.geojson` (built by `pipeline/buildings.py`, with
  `pipeline/building_tiers.py` and `pipeline/building_pointcloud.py`) is a **derived database of
  OpenStreetMap data under ODbL 1.0** (© OpenStreetMap contributors) **and City of Melbourne Open
  Data** (2023 Building Footprints, 3D Point Cloud 2018, Municipal boundary; CC BY 4.0, modified).
  Any public use must keep both attributions: "© OpenStreetMap contributors" and the combined map
  credit above. Extent: `BUILDINGS_AREA` (City of Melbourne ∪ `BUILDINGS_BBOX`). Each feature is a
  building **piece** drawn from `min_height_m` to `height_m` (metres above the model's flat
  ground). Inside the City of Melbourne the pieces are the City of Melbourne footprints' stacked
  tiers (geometry and heights from that dataset, tidied and merged as documented in
  `pipeline/building_tiers.py`); `height_source` is `com_pointcloud_2018` where the point cloud's
  roof level agrees with the tier top within 3 m (**confirmed** by the cloud, the height itself is
  City of Melbourne's), else `com_open_data`. The cloud never replaces a City of Melbourne height
  (its rule allows it only for a City of Melbourne capture older than May 2018, and there is none).
  OSM buildings not covered by those tiers, and everything outside the City of Melbourne, keep the
  OSM height chain (`osm_height`, `osm_levels`, `com_open_data`, `type_default`). Space under OSM
  elevated pieces (OSM `min_height`) and along drivable at-grade roads through buildings
  (`roads_3d.geojson`) is cleared up to the OSM `min_height` / `maxheight`, else an **assumed**
  4.5 m clearance. OSM parts that are underground only (`level` < 0) are not drawn.
- `data/processed/checks/buildings_heights.json` is a **verification report, not a contract
  file**: counts, every point-cloud comparison that disagrees by more than 3 m (with the City of
  Melbourne capture date), the datum offset between the cloud and City of Melbourne elevations,
  elevated tiers, carved pieces, City of Melbourne structures dropped as out of date, and the
  landmark table. Derived from OpenStreetMap (ODbL 1.0) and City of Melbourne Open Data (CC BY 4.0).

**Tower crowns (`pipeline/building_pointcloud.py`, `crowns` / `crown_of` / `crane_like`):** City of
Melbourne tier tops stop at the main roof, so crowns, masts and roof frames that the 2018 point
cloud sees are missing (101 Collins St: 194 m in the footprints, about 235 m in the cloud). For
each structure at least 25 m tall whose cloud confirms the City of Melbourne tiers (`shows_structure`),
cloud points over its upper tiers (tops at least 0.8 x the structure's top) that rise more than 1 m
above the City of Melbourne top form a candidate crown. It is accepted as an extra piece only if
its top (98th percentile of those points, so antenna tips are ignored) exceeds the City of Melbourne
top by more than 5 m, it has at least 30 points, a footprint of at least 4 m2 and 1 % of the upper
roof, and at least 1 point per m2. A long thin, sparsely filled hull (aspect 4 or more and cell
occupancy under 0.3) is rejected as **crane-like**: a tower crane jib in the May 2018 capture, not
a building. Rejected as crane-like: 807044 (Melbourne Central office tower, aspect 28, occupancy
0.07), 807685, 817678, 819289. The cloud does not show 819200 and 819347, so they get no crown.
Each crown is a piece `c<structure>_crown` with `height_source: com_pointcloud_2018` and
`min_height_m` equal to the parent tier's top (52 crowns). Crown heights are measured from a 2018
capture. Report: `data/processed/checks/buildings_heights.json` -> `pointcloud.crowns`.

**Tunnel documentation notes (Stage B):**

- **Domain Tunnel depth.** The drawn depth under the Yarra (about -21.4 to -27.5 m, way 8066126) is
  an **estimate** from the rule of -10 m x layer, kept below the river-bed limit (-17 m = 6 m river
  depth + 5 m cover + 6 m tunnel roof). The point cloud cannot see underground. Public descriptions
  (Wikipedia, "Domain Tunnel": road level at most 40 m deep, a shallow cut-and-cover river
  crossing) suggest the real crossing is shallower; the drawn depth is not a measurement.
- **Way 203310289** is tagged `tunnel`, `indoor=yes`, `level=0`: a ground-floor road inside the
  Southern Cross buildings. It is deliberately **not lowered**. It sits 2.3-5.7 m above street
  level only because it joins a building passage from the estimated service bridge 1455122808
  (a flat-ground artefact), and it draws as an X-ray tunnel above street level.
- **`trench_crossings` for ways 26619046 / 5234953** reports an inherited depth of 6.06 m. The drawn
  heights are correct; the recorded value is inherited from the neighbouring way and is not a
  measurement of that crossing.

## Phase 3: SCATS traffic counts (data-engineer, 2026-10-27)

Added before any of these files were used. Licences confirmed on 2026-10-27 from the publisher's own
CKAN metadata (`https://opendata.transport.vic.gov.au/api/3/action/package_show?id=<dataset>`:
`license_id: CC-BY-4.0`, `license_url: https://creativecommons.org/licenses/by/4.0/`); a copy of
each metadata response is kept in `data/raw/scats/ckan/`. All are aggregated counts or engineering
documents: no personal data, no imagery of people or vehicles.

| Dataset | Publisher | URL | Licence | Attribution required | Downloaded | Used for | Raw file(s) in `data/raw/` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Traffic Signal Volume Data (dataset id `traffic-signal-volume-data`): SCATS loop-detector vehicle counts, 15-minute bins per detector per day, one CSV per day (`VSDATA_YYYYMMDD.csv`) inside monthly ZIPs. Only single daily CSVs are fetched, each with HTTP range requests out of its monthly ZIP (central directory + one member), cached, no retries: 28 daily files so far (every day of the weeks of 14 Sep and 5, 12, 19 Oct 2026, see "Days used" below). Metadata notes "a maximum of 24 Volume Detectors are available per site" | Department of Transport and Planning, State of Victoria (Transport Victoria open data, "Roads") | https://opendata.transport.vic.gov.au/dataset/traffic-signal-volume-data (September 2026 ZIP: `.../resource/74a90543-78fa-4759-8ab0-aea64748e698/download/traffic_signal_volume_data_september_2026.zip`; October 2026 ZIP: `.../resource/001ace40-280b-462d-b580-7a79e70185fb/download/traffic_signal_volume_data_october_2026.zip`; full URLs in `pipeline/scats_download.py`) | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/, publisher metadata) | Yes: "Traffic counts: Contains Department of Transport and Planning (Victoria) Traffic Signal Volume Data, CC BY 4.0, modified" | 2026-10-27 | `counts.parquet`: the real 15-minute counts of ONE day, **Friday 23 October 2026** (the calibration target for the `am_peak` demand, Phase 3), summed per approach over its stop-line loops and mapped to SUMO edges; Fri 16 Oct, Fri 9 Oct and Fri 18 Sep 2026 are used only as QC comparison days (faulty detectors are dropped, never filled from them). The pipeline is date-generic (`--from/--to`) | `scats/VSDATA_YYYYMMDD.csv.gz` (gzip of the extracted CSV, byte-identical when unzipped), `scats/MANIFEST.json` (zip URL, member, offset, sizes, CRC, sha256) |
| Victorian Traffic Signals (dataset id `victorian-traffic-signals`): register of signal sites, `SITE_NO`, `SITE_NAME`, `TYPE`, `MUNICIPALITY`, `LATITUDE`, `LONGITUDE` (resource last modified 2026-08-12) | Department of Transport and Planning, State of Victoria | https://opendata.transport.vic.gov.au/dataset/victorian-traffic-signals (CSV: `https://opendata.transport.vic.gov.au/dataset/923af458-363d-469f-bc5e-84746a80b9a2/resource/1036a318-8867-4dc6-bd84-fc3bcd6cbdb9/download/victorian_traffic_signals.csv`) | CC BY 4.0 (publisher metadata) | Yes: as above | 2026-10-27 | Which SCATS sites lie in `SIM_BBOX`, their names and coordinates (to find the matching SUMO junction) | `scats/victorian_traffic_signals.csv` |
| Traffic Signal Configuration Data Sheets (dataset id `traffic-signal-configuration-data-sheets`): DTP operation sheets per signal site (rasterised PDF or Excel-HTML export: detector map, detector functions, approach definitions, phasing). 27 ZIPs of 0.1–3.6 GB (all last modified 2026-03-20). Only the sheets of the sites used are fetched, via HTTP range requests (central directory + one request per member), no retries | Department of Transport and Planning, State of Victoria | https://opendata.transport.vic.gov.au/dataset/traffic-signal-configuration-data-sheets (ZIP URLs in `pipeline/scats_download.py`) | CC BY 4.0 (publisher metadata) | Yes: "Contains Department of Transport and Planning (Victoria) Traffic Signal Configuration Data Sheets, CC BY 4.0" | 2026-10-27 | **Detector → approach/lane mapping** (which detectors are vehicle stop-line loops and which approach they sit on), read from these published engineering diagrams under George's approved exception (CLAUDE.md constraint 1, 2026-10-27); our own provenance copy for the 10 % re-check and George's spot-check. Signal timings are **not** used in Phase 3 (deferred) | `scats/sheets/<range>/<SITE_NO>.pdf` or `.htm` + `<SITE_NO>_files/`, `scats/sheets/MANIFEST.json` |
| Historical Annual Average Daily Traffic Volume, 2019 file (dataset id `historical-annual-average-daily-traffic-volume`, "No Longer Updated", ends 2019): AADT per direction on declared-road segments | Department of Transport and Planning, State of Victoria | https://opendata.transport.vic.gov.au/dataset/historical-annual-average-daily-traffic-volume (GeoJSON: `https://opendata.transport.vic.gov.au/dataset/26fafd1a-8d59-4da0-93cd-29f371147d8f/resource/425799c9-658c-41cf-b9b0-6c9a145856cf/download/yearly_aadt_volume_2019.geojson`) | CC BY 4.0 (publisher metadata) | Yes: as above | 2026-10-27 | **Verification only, never used for fitting**: (1) an order-of-magnitude sanity check of SCATS approach daily totals against 2019 AADT where a segment matches (`data/processed/checks/counts_report.json`); (2) the freeway/arterial reference `data/processed/checks/freeway_reference.json` (segment, direction, AADT, heavy-vehicle %, matched SUMO edge IDs on CityLink, West Gate Fwy, Kings Way, Wurundjeri Way and other arterials in `SIM_BBOX`). 2019 is pre-COVID and pre-West Gate Tunnel: an old, coarse reference | `scats/yearly_aadt_volume_2019.geojson` |
| TIRTL Traffic Counts and Classification (dataset id `tirtl-traffic-counts`): infra-red roadside loggers, vehicle volumes per 15 minutes by Austroads class, speed bin, site and heading; `tirtl_sites.csv` site table. Only sites **504 / 505** (West Gate Bridge inbound / outbound) are used, for the counts target day (23 Oct 2026) and its first QC day (16 Oct 2026, comparison only); each monthly ZIP is read with HTTP range requests (central directory + the members needed), no retries | Department of Transport and Planning, State of Victoria | https://opendata.transport.vic.gov.au/dataset/tirtl-traffic-counts (site table: `https://opendata.transport.vic.gov.au/dataset/e2d78fb5-e16d-43b9-bcdc-607d9b4855f5/resource/1f685833-24fd-4eb0-af11-2e7cfc94da74/download/tirtl_sites.csv`; monthly ZIP URLs in `pipeline/freeway_checks.py`) | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/, publisher CKAN metadata `license_id: CC-BY-4.0`, checked 2026-10-27) | Yes: "Contains Department of Transport and Planning (Victoria) TIRTL Traffic Counts, CC BY 4.0, modified" | 2026-10-27 | **Verification only, never used for fitting**: 15-minute West Gate Bridge volumes in `data/processed/checks/freeway_reference.json`, labelled as an **upstream bound** (about 3 km west of our boundary, with exits in between) on what the West Gate Fwy entry can carry. No vehicle-level data; the dataset is aggregated 15-minute counts | `tirtl/tirtl_sites.csv`, `tirtl/TIRTLDATA_20261023.csv.gz`, `tirtl/TIRTLDATA_20261016.csv.gz` (whole daily files, gzipped, unmodified), `tirtl/MANIFEST.json` |

**Days used (Australia/Melbourne).** Deliverable changed by the orchestrator (George, 2026-10-27)
from a typical-weekday median to "yesterday's traffic": **counts.parquet holds one real day,
Friday 23 October 2026**, the most recent normal working weekday published on 2026-10-27 (the
October 2026 ZIP, last modified 2026-10-26T13:03Z, held 1-25 October; 24-25 October are a
weekend; Monday 26 October was not yet published). It is in school Term 4 and not a public
holiday. **QC-only days** (never used to fill or replace a count): the same weekday in the three
most recent earlier weeks that are normal working days, Fri 16 Oct, Fri 9 Oct and Fri 18 Sep 2026
(Fri 2 Oct and Fri 25 Sep are in the school holidays, 19 Sep - 4 Oct). Also cached in
`data/raw/scats/` (fetched under the first plan, before the change; used for nothing but the
pipeline's date-generic test): every day of the weeks of 14 Sep and 5, 12 and 19 Oct 2026
(28 daily files, ~4.5 MB each zipped, ~30 MB as CSV, ~4.7 MB as stored `.csv.gz`).

TIRTL: the target day's file (`TIRTLDATA_20261023.csv`, 4.5 MB zipped, 56 MB as CSV) and the 16 Oct
comparison file are cached as `data/raw/tirtl/TIRTLDATA_<date>.csv.gz` with `tirtl_sites.csv`
and a `MANIFEST.json`. Site 504 (inbound) is marked unreliable in the AM peak in
`freeway_reference.json` (it collapses mid-peak on both days).

**References (facts cited, no data downloaded):**

| Reference | URL | Licence | Read | Used for |
| --- | --- | --- | --- | --- |
| Victorian Government, "School term dates and holidays in Victoria" | https://www.vic.gov.au/school-term-dates-and-holidays-victoria | © State of Victoria; dates used as single facts | 2026-10-27 | 2026 terms: Term 3 13 July – 18 September, Term 4 5 October – 18 December (so 19 September – 4 October are holidays) |
| Business Victoria, "Public holidays in Victoria for 2026" | https://business.vic.gov.au/business-information/public-holidays/victorian-public-holidays-2026 (direct fetch refused with HTTP 403 on 2026-10-27; read via the search engine's summary of that page) | © State of Victoria; dates used as single facts | 2026-10-27 | Melbourne Cup Tuesday 3 November 2026; Friday before the AFL Grand Final not fixed on the page (a Friday, so outside Tue–Thu) |

**Derived reference, not a source: George's AI first pass on the configuration sheets.** George's
own AI (Antigravity) read the DTP Traffic Signal Configuration Data Sheets above into per-site JSON
(`~/Downloads/signal_sheets/<SITE_NO>.json`: detector number, type, approach, lane, movement, page;
`index.csv`). It is an AI reading of a CC BY 4.0 source, **untrusted input**: read only as data,
nothing executed from it, and nothing from it enters `data/` unless it agrees with our own
downloaded copy of the sheet. In practice the data engineer read every counted site's detector
map from our own sheet copy, because the AI's direction labels were often reversed (e.g. Lonsdale
St eastbound/westbound swapped at 2902, 2903, 2904, 2909) and some loops were mistyped (hook-turn
boxes and queue loops as stop-line loops). A row is cited `source: "george_ai_verified"` only
where the AI gave the same type, street and direction for that detector as our own reading;
every other row is `source: "data_engineer_read"`. A random 10 % of the mapped sites was then
re-read (see `counts_report.json` -> `recheck_10pct`).

**Derived reference, not a source (task 3C, 2026-10-27): the same AI output for signal programs and
lane arrows.** The whole AI folder (221 `<site>.json`, `index.csv`, `tram_stops.csv/json`,
`diagrams/` page renders) was copied unchanged to `data/raw/signal_sheets_ai/` on 2026-10-27
(`MANIFEST.json` there: sha256 per file). Fields used: phase sequence and signal groups per phase,
phase timings, pedestrian timings, lane layouts. It is an AI reading of the CC BY 4.0 DTP sheets
above, validated by `pipeline/signal_sheets.py` before use: automatic checks on all 221 sites
(schema, ranges, phase consistency, register/bbox, SUMO junction names, lane counts, OSM
`turn:lanes`, headings; htm sheets' timing tables parsed as text and compared), and a fixed-seed
random re-read of 25 sites against our own sheet copy, with a fixed error-rate rule per field
group (`data/processed/checks/signal_sheets_validation.json`). Nothing from it is used where the
rule rejected the field group; every value in the output carries its provenance
(`ai_checked` / `ai_unverified` / `re_read` / `rejected` / `not_read` / `no_sheet`).
`tram_stops.*` (OSM-derived) is not used. Our own sheet copy gained 12 POS sheets for this task
(3416, 4515, 2927, 1425, 2393, 3166, 4450, 4534, 4535, 4582, 4726, 4900), fetched the same way
(one range request per site; `scats/sheets/MANIFEST.json`).

**Derived outputs:**

- `data/processed/counts.parquet` (built by `pipeline/counts.py`) is derived from DTP Traffic Signal
  Volume Data (CC BY 4.0, modified): real 15-minute vehicle counts per signalised approach for the
  day(s) built (default: Friday 23 October 2026), summed over the approach's stop-line loops,
  with the SUMO edge ID it was mapped to. No count is ever filled from another day. Attribution: "Traffic counts: Contains
  Department of Transport and Planning (Victoria) Traffic Signal Volume Data, CC BY 4.0, modified".
- `data/processed/checks/counts_report.json`, `data/processed/checks/counts_spotcheck/` (sheet
  crops from our own copy of the DTP sheets, CC BY 4.0) and `data/processed/checks/freeway_reference.json`
  (AADT 2019 and TIRTL, CC BY 4.0, modified; check only) are **verification reports, not contract
  files**.
- `pipeline/reference/scats_detector_map.json`: the detector -> approach map, one row per counted
  detector with its sheet file, page and source (`data_engineer_read` / `george_ai_verified`);
  derived from the DTP Traffic Signal Configuration Data Sheets (CC BY 4.0).
- `pipeline/reference/signal_sheets_verified.json`: per signal site, phase sequence, signal groups
  and timings per phase, signal-group movements (priority sites), pedestrian timings and lane
  layouts, each field group with its provenance and sheet page; built by `pipeline/signal_sheets.py`
  from our own reading (`pipeline/reference/signal_sheets_reread.txt`), the htm sheets' text tables
  and, where the rule allows, the AI output above. Derived from the DTP Traffic Signal
  Configuration Data Sheets (CC BY 4.0, modified). `data/processed/checks/signal_sheets_validation.json`
  and `data/processed/checks/signal_sheets_spotcheck/` (downscaled sheet page images, CC BY 4.0)
  are verification reports.

## Phase 3: regulatory turn signs (review session, 2026-10-28)

- **Sign Panels (Non Parking)** — City of Melbourne Open Data
  - URL: https://data.melbourne.vic.gov.au/explore/dataset/sign-panels-non-parking/
  - Licence: CC BY (dataset metadata; modified 2026-09-03)
  - Accessed: 2026-10-28, via the Opendatasoft records API (no bulk download; queried panels
    whose `panel_text` matches turn wording, and all panels within 60 m of the Phase 3
    checklist corners). Query responses kept only in the session scratchpad.
  - Used for: verifying turn rules at signal sites where OSM is silent and the DTP sheets are
    ambiguous (docs/review/local-knowledge-checklist-phase3.md). Verified so far:
    R2-21 "right turn from left lane" (hook turn), asset 1633286, Queen St at Bourke St;
    R2-6 "No Right Turn" assets 1610792 and 1344763, Little Collins St at King St (both
    approaches, no time plates). Attribution: "Turn signage: City of Melbourne Sign Panels
    (Non Parking), CC BY".
  - Caveat: covers City of Melbourne pole assets only; signs on signal mast arms (state
    assets) are absent, so a missing sign never proves a turn is allowed.
