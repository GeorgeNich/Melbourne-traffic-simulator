import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Map as MapLibreMap } from "maplibre-gl";
import type { MapRef } from "react-map-gl/maplibre";
import { DEFAULT_PRESET, FLY_DURATION_MS, getPreset, type PresetId } from "./camera/presets";
import { ControlPanel } from "./components/ControlPanel";
import { FixtureBanner } from "./components/FixtureBanner";
import { FpsMeter } from "./components/FpsMeter";
import { NoTrafficNote, PlaybackControls } from "./components/PlaybackControls";
import { useBuildings } from "./data/useBuildings";
import { basemapStyle } from "./map/basemap";
import { buildingsLayers } from "./map/buildingsLayer";
import { extendFarPlaneBelowGround } from "./map/farPlane";
import { capStillRenderRate } from "./map/renderCap";
import { heightsViewFromQuery, roadsLayers, tunnelLayers } from "./map/roadsLayer";
import { useRoads } from "./data/useRoads";
import { useTraffic } from "./data/useTraffic";
import { useScenarios } from "./data/useScenarios";
import { type RoadsForSpeeds, useSpeeds } from "./data/useSpeeds";
import { type FrameLayers, TwinMap } from "./map/TwinMap";
import { PlaybackClock } from "./traffic/playback";
import { trafficRenderer } from "./traffic/trafficLayers";
import { speedRenderer } from "./traffic/speedLayers";
import { binAt, edgeSpeed } from "./traffic/speeds";
import { BUILDING_OPACITY, palette } from "./theme/palette";
import { APP_TITLE } from "./config/app";
import { scenarioLabel } from "./config/scenarios";

/** The committed base style (no fetch), built once. The whole look (traffic, roads, tunnels, buildings): palette.ts. */
const BASEMAP_STYLE = basemapStyle();
/** Dev only: `?heights=source` tints elevated roads by z_source (measured vs estimated). Ignored in builds. */
const HEIGHTS_VIEW = heightsViewFromQuery(window.location.search, import.meta.env.DEV);

export function App() {
  const mapRef = useRef<MapRef>(null);
  const [preset, setPreset] = useState<PresetId | null>(DEFAULT_PRESET);
  const [opacity, setOpacity] = useState<number>(BUILDING_OPACITY.default);
  // Zoom in 0.25 steps: drives the roofline fade without rebuilding layers on every camera frame.
  const [zoomStep, setZoomStep] = useState<number>(() => getPreset(DEFAULT_PRESET).view.zoom);
  const buildings = useBuildings();
  const roads = useRoads();
  // The replayed scenario: traffic.json's (the default: am_peak when synced, else baseline) until the switch picks another.
  const scenarios = useScenarios();
  const [picked, setPicked] = useState<string | null>(null);
  const manifest = scenarios.status === "ready" ? (scenarios.options.find((m) => m.scenario === picked) ?? scenarios.initial) : undefined;
  // Which below-ground cars are in a tunnel (X-ray) comes from roads_3d, so traffic is prepared once the roads are in.
  const traffic = useTraffic(manifest, roads.status === "ready" ? roads.tunnelAt : roads.status === "error" ? null : undefined);
  const [clock] = useState(() => new PlaybackClock(0));
  const [showTunnels, setShowTunnels] = useState(true);
  const [showSpeeds, setShowSpeeds] = useState(true);
  // Road speeds (edge_stats) for the replayed scenario; tunnel edges follow their way's roads_3d tunnel flag.
  const speedRoads = useMemo<RoadsForSpeeds | null | undefined>(
    () => (roads.status === "ready" ? { wayTunnel: roads.wayTunnel, tunnelAt: roads.tunnelAt } : roads.status === "error" ? null : undefined),
    [roads],
  );
  const speeds = useSpeeds(manifest, speedRoads, traffic.status === "ready" ? traffic.meta.duration_s : undefined);
  // Dev only: the map, for the passive fps readout.
  const [devMap, setDevMap] = useState<MapLibreMap | null>(null);

  // The playback clock is attached only while there is traffic to play; it requests animation frames only while
  // playing with the page visible (redraw on demand, playback.ts). It replays the trips' own time range (am_peak
  // starts at 3600 s, not 0); a scenario switch moves it to the new range's start.
  const rangeStart = traffic.status === "ready" ? traffic.range.start : 0;
  const rangeEnd = traffic.status === "ready" ? traffic.range.end : 0;
  useEffect(() => {
    if (!(rangeEnd > rangeStart)) return;
    clock.setRange(rangeStart, rangeEnd);
    // Dev/test hook: Playwright pauses, seeks and changes speed through this. Not present in builds.
    if (import.meta.env.DEV) window.__twinPlayback = clock;
    return clock.start();
  }, [clock, rangeStart, rangeEnd]);

  useEffect(() => {
    // Dev/test hook: Playwright reads the road speeds the layers draw. Not present in builds.
    if (!import.meta.env.DEV || speeds.status !== "ready") return;
    const index = new Map(speeds.table.edgeIds.map((id, i) => [id, i]));
    window.__twinSpeeds = {
      table: speeds.table,
      speedAt: (id, t) => {
        const e = index.get(id);
        return e === undefined ? null : edgeSpeed(speeds.table, e, binAt(t, speeds.table.nBins, speeds.table.binS));
      },
    };
  }, [speeds]);

  // Built once per dataset; each frame only swaps currentTime (see trafficLayers.ts) or, for the road speeds, the
  // colours when the 60 s bin changes (speedLayers.ts). Road speeds draw first, so the cars glow over their roads.
  const renderTraffic = useMemo(() => (traffic.status === "ready" ? trafficRenderer(traffic.prepared) : null), [traffic]);
  const renderSpeeds = useMemo(() => (speeds.status === "ready" ? speedRenderer(speeds.pieces, speeds.table) : null), [speeds]);
  const frame = useMemo<FrameLayers | undefined>(
    () =>
      renderTraffic
        ? { clock, layers: (t) => [...(renderSpeeds ? renderSpeeds(t, showTunnels, showSpeeds) : []), ...renderTraffic(t, showTunnels)] }
        : undefined,
    [clock, renderTraffic, renderSpeeds, showTunnels, showSpeeds],
  );

  const buildingLayers = useMemo(
    () => (buildings.status === "ready" ? buildingsLayers(buildings.polygons, buildings.rooflines, buildings.towerEdges, opacity, zoomStep) : []),
    [buildings, opacity, zoomStep],
  );
  // Roads are drawn after the buildings (see roadsLayers for the depth rules). X-ray tunnels are drawn before them, so
  // the translucent city tints over the tunnels; "Show tunnels" hides them together with the tunnel cars.
  const roadLayers = useMemo(() => (roads.status === "ready" ? roadsLayers(roads.geometry, HEIGHTS_VIEW) : []), [roads]);
  const tunnelRibbons = useMemo(() => (roads.status === "ready" ? tunnelLayers(roads.geometry.tunnel, showTunnels) : []), [roads, showTunnels]);
  const layers = useMemo(() => [...tunnelRibbons, ...buildingLayers, ...roadLayers], [tunnelRibbons, buildingLayers, roadLayers]);

  const flyTo = useCallback((id: PresetId) => {
    const { view } = getPreset(id);
    setPreset(id);
    mapRef.current?.flyTo({
      center: [view.longitude, view.latitude],
      zoom: view.zoom,
      pitch: view.pitch,
      bearing: view.bearing,
      duration: FLY_DURATION_MS,
      essential: true,
    });
  }, []);

  const onLoad = useCallback(() => {
    const map = mapRef.current?.getMap();
    if (!map) return;
    // Keep the far plane below the deepest tunnel, so X-ray tunnels and tunnel cars are never clipped (farPlane.ts).
    extendFarPlaneBelowGround(map);
    // With the camera still, render at most 60 times a second, also on 120 Hz displays (renderCap.ts).
    capStillRenderRate(map);
    // Dev/test hook: Playwright drives the camera and reads timings through this. Not present in builds.
    if (import.meta.env.DEV) {
      window.__twinMap = map;
      setDevMap(map);
    }
  }, []);

  const trafficSettled = traffic.status === "ready" || traffic.status === "none";
  // Speeds wait for the traffic meta (duration); with no traffic there are no speeds to wait for.
  const speedsSettled = speeds.status === "ready" || speeds.status === "none" || traffic.status === "none";
  const onIdle = useCallback(() => {
    // Ready only when every dataset loaded and passed the contract (traffic and speeds may be "none yet"): a roads,
    // traffic or speeds failure must fail the e2e run too.
    if (buildings.status === "ready" && roads.status === "ready" && trafficSettled && speedsSettled && !window.__twinReady) {
      window.__twinReady = true;
      performance.mark("twin:ready");
    }
  }, [buildings.status, roads.status, trafficSettled, speedsSettled]);

  const buildingsStatus =
    buildings.status === "loading"
      ? "Loading buildings…"
      : buildings.status === "error"
        ? `Buildings failed: ${buildings.message}`
        : `${buildings.featureCount.toLocaleString()} building pieces`;
  const roadsStatus =
    roads.status === "loading"
      ? "loading roads…"
      : roads.status === "error"
        ? `Roads failed: ${roads.message}`
        : `${roads.elevatedWays.toLocaleString()} elevated roads`;
  const status = `${buildingsStatus} · ${roadsStatus}`;
  // Contract errors (dev validation) and load failures for both datasets share one alert panel.
  const alerts = [
    ...(scenarios.status === "error" ? [`[traffic] ${scenarios.message}`] : []),
    ...(buildings.status === "error" && buildings.validation ? buildings.validation.errors.map((e) => `[buildings] ${e}`) : []),
    ...(roads.status === "error"
      ? roads.validation
        ? roads.validation.errors.map((e) => `[roads] ${e}`)
        : [`[roads] ${roads.message}`]
      : []),
    ...(traffic.status === "error" ? (traffic.errors ?? [traffic.message]).map((e) => `[traffic] ${e}`) : []),
    ...(speeds.status === "error" ? (speeds.errors ?? [speeds.message]).map((e) => `[speeds] ${e}`) : []),
  ];
  const fixtureBuildings = buildings.status === "ready" && buildings.isTestFixture;
  const fixtureTrips = traffic.status === "ready" && traffic.isTestFixture;

  return (
    <main style={{ position: "fixed", inset: 0, background: palette.background, overflow: "hidden" }}>
      <TwinMap
        ref={mapRef}
        mapStyle={BASEMAP_STYLE}
        initialView={getPreset(DEFAULT_PRESET).view}
        layers={layers}
        frame={frame}
        onLoad={onLoad}
        onIdle={onIdle}
        onUserMove={() => setPreset(null)}
        onZoom={(z) => setZoomStep(Math.round(z * 4) / 4)}
      />
      <ControlPanel
        title={APP_TITLE}
        activePreset={preset}
        onPreset={flyTo}
        opacity={opacity}
        onOpacity={setOpacity}
        speeds={speeds.status === "ready" ? { on: showSpeeds, onChange: setShowSpeeds } : speeds.status === "none" ? "none" : null}
        scenarios={
          scenarios.status === "ready" && scenarios.options.length > 1 && manifest
            ? { options: scenarios.options.map((m) => ({ id: m.scenario, label: scenarioLabel(m.scenario) })), active: manifest.scenario, onChange: setPicked }
            : null
        }
        status={status}
      />
      {(fixtureBuildings || fixtureTrips) && <FixtureBanner buildings={fixtureBuildings} trips={fixtureTrips} />}
      {traffic.status === "ready" && (
        <PlaybackControls
          clock={clock}
          meta={traffic.meta}
          prepared={traffic.prepared}
          showTunnels={showTunnels}
          onShowTunnels={setShowTunnels}
        />
      )}
      {traffic.status === "none" && <NoTrafficNote scenario={traffic.scenario} />}
      {alerts.length > 0 && (
        <pre
          role="alert"
          style={{
            position: "absolute",
            left: 12,
            bottom: 40,
            maxWidth: "calc(100% - 24px)",
            maxHeight: "40vh",
            overflow: "auto",
            margin: 0,
            padding: 12,
            background: palette.ui.panel,
            color: palette.ui.warningText,
            font: "12px ui-monospace, Menlo, monospace",
          }}
        >
          {alerts.join("\n")}
        </pre>
      )}
      {import.meta.env.DEV && <FpsMeter map={devMap} />}
    </main>
  );
}
