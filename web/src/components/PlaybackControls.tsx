import { type CSSProperties, useSyncExternalStore } from "react";
import { type Demand, isPlaceholderDemand, type TripsMeta } from "../data/tripsContract";
import { demandBadge } from "../config/scenarios";
import { formatClock, formatLocalClock, type PlaybackClock, SPEEDS } from "../traffic/playback";
import { activeCars, carsInTunnels, type PreparedTraffic } from "../traffic/prepare";
import { palette } from "../theme/palette";

const panel: CSSProperties = {
  position: "absolute",
  left: "50%",
  bottom: 36, // above the one-line attribution bar
  transform: "translateX(-50%)",
  // max-content: positioned at left 50%, shrink-to-fit would otherwise stop at half the viewport and wrap the row.
  width: "max-content",
  maxWidth: "calc(100% - 120px)", // clear of the navigation control (bottom left)
  padding: "8px 12px",
  background: palette.ui.panel,
  border: `1px solid ${palette.ui.border}`,
  borderRadius: 6,
  color: palette.ui.text,
  font: "13px system-ui, sans-serif",
  display: "flex",
  alignItems: "center",
  gap: 10,
  flexWrap: "wrap", // only on narrow windows: one line at 1440 px
  justifyContent: "center",
  zIndex: 5,
};

const button = (active: boolean): CSSProperties => ({
  padding: "4px 9px",
  font: "12px system-ui, sans-serif",
  fontVariantNumeric: "tabular-nums",
  color: active ? palette.background : palette.ui.text,
  background: active ? palette.ui.accent : "transparent",
  border: `1px solid ${active ? palette.ui.accent : palette.ui.border}`,
  borderRadius: 4,
  cursor: "pointer",
});

/** Muted label for the demand source: never a traffic colour (those are reserved for cars and roads). */
const badge: CSSProperties = {
  padding: "3px 8px",
  border: `1px solid ${palette.ui.textDim}`,
  borderRadius: 10,
  color: palette.ui.text,
  font: "600 11px system-ui, sans-serif",
  letterSpacing: "0.04em",
  textTransform: "uppercase",
  whiteSpace: "nowrap",
};

const PlayIcon = () => (
  <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
    <path d="M0 0 L10 6 L0 12 Z" fill="currentColor" />
  </svg>
);
const PauseIcon = () => (
  <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
    <rect x="0" y="0" width="3.5" height="12" fill="currentColor" />
    <rect x="6.5" y="0" width="3.5" height="12" fill="currentColor" />
  </svg>
);

/** Tooltip for the placeholder badge, by demand label. */
export function demandNote(demand: Demand): string {
  if (demand === "test_fixture") return "Hand-made test cars on real roads, not simulated traffic.";
  if (demand === "placeholder_random") return "Random placeholder demand: not calibrated to traffic counts yet.";
  return "Demand calibrated to open traffic counts.";
}

interface Props {
  clock: PlaybackClock;
  meta: TripsMeta;
  prepared: PreparedTraffic;
  showTunnels: boolean;
  onShowTunnels: (on: boolean) => void;
}

export function PlaybackControls({ clock, meta, prepared, showTunnels, onShowTunnels }: Props) {
  const s = useSyncExternalStore(clock.subscribeUi, clock.getSnapshot);
  const placeholder = isPlaceholderDemand(meta.demand);
  // Real Melbourne time when the meta has start_local (am_peak); otherwise mm:ss from scenario start.
  const local = formatLocalClock(s.time, meta.start_local);
  // With "Show tunnels" off, cars in tunnels aren't drawn, so they aren't counted; a note says how many are hidden.
  const underground = carsInTunnels(prepared, s.time);
  const cars = activeCars(prepared, s.time) - (showTunnels ? 0 : underground);
  return (
    <div role="region" aria-label="Traffic playback" data-testid="playback" style={panel}>
      <button
        type="button"
        data-testid="play-pause"
        aria-label={s.playing ? "Pause" : "Play"}
        style={{ ...button(false), width: 30, display: "inline-flex", justifyContent: "center", padding: "6px 0" }}
        onClick={() => clock.toggle()}
      >
        {s.playing ? <PauseIcon /> : <PlayIcon />}
      </button>
      <input
        type="range"
        aria-label="Playback position"
        min={s.start}
        max={s.duration}
        step={0.5}
        value={s.time}
        onChange={(e) => clock.seek(Number(e.target.value))}
        style={{ accentColor: palette.ui.accent, width: 200 }}
      />
      <span
        data-testid="sim-clock"
        title={local ? "Melbourne local time" : "Simulation time from scenario start"}
        style={{ fontVariantNumeric: "tabular-nums", minWidth: 92, whiteSpace: "nowrap" }}
      >
        {local ?? (
          <>
            {formatClock(s.time)} <span style={{ color: palette.ui.textDim }}>/ {formatClock(s.duration)}</span>
          </>
        )}
      </span>
      <div role="group" aria-label="Playback speed" style={{ display: "flex", gap: 4 }}>
        {SPEEDS.map((v) => (
          <button key={v} type="button" aria-pressed={s.speed === v} style={button(s.speed === v)} onClick={() => clock.setSpeed(v)}>
            {v}×
          </button>
        ))}
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 6, color: palette.ui.textDim, whiteSpace: "nowrap" }}>
        <input
          type="checkbox"
          checked={showTunnels}
          onChange={(e) => onShowTunnels(e.target.checked)}
          style={{ accentColor: palette.ui.accent }}
        />
        Show tunnels
      </label>
      <span data-testid="cars" style={{ color: palette.ui.textDim, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>
        {cars.toLocaleString()} {cars === 1 ? "car" : "cars"}
      </span>
      {!showTunnels && underground > 0 && (
        <span data-testid="cars-hidden" title="Cars in tunnels, hidden while Show tunnels is off" style={{ color: palette.ui.textDim, whiteSpace: "nowrap", marginLeft: -4 }}>
          +{underground.toLocaleString()} in tunnels
        </span>
      )}
      <span data-testid={placeholder ? "placeholder-badge" : "demand-badge"} title={demandNote(meta.demand)} style={badge}>
        {demandBadge(meta)}
      </span>
    </div>
  );
}

/** Shown instead of the controls when no trips have been exported yet. */
export function NoTrafficNote({ scenario }: { scenario: string }) {
  return (
    <div role="status" data-testid="traffic-note" style={{ ...panel, color: palette.ui.textDim, fontSize: 12 }}>
      No traffic yet: sim/out/trips_{scenario}.json has not been exported.
    </div>
  );
}
