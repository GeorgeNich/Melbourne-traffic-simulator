import type { CSSProperties } from "react";
import { CAMERA_PRESETS, type PresetId } from "../camera/presets";
import { BUILDING_OPACITY, cssRgba, palette, SPEED, type SpeedClass } from "../theme/palette";

interface Props {
  title: string;
  activePreset: PresetId | null;
  onPreset: (id: PresetId) => void;
  opacity: number;
  onOpacity: (value: number) => void;
  /** The "Road speeds" toggle; "none" = no edge stats for this scenario; null = still loading or failed. */
  speeds: { on: boolean; onChange: (on: boolean) => void } | "none" | null;
  status: string;
  /** The scenario switch; null when there is only one scenario. */
  scenarios: { options: { id: string; label: string }[]; active: string; onChange: (id: string) => void } | null;
}

const LEGEND: { cls: SpeedClass; label: string; title: string }[] = [
  { cls: "flowing", label: "flowing", title: `speed at least ${SPEED.thresholds.flowing * 100}% of the limit` },
  { cls: "slowing", label: "slowing", title: `${SPEED.thresholds.slowing * 100}–${SPEED.thresholds.flowing * 100}% of the limit` },
  { cls: "jammed", label: "jammed", title: `under ${SPEED.thresholds.slowing * 100}% of the limit` },
];

/** "Road speeds" on/off with a one-line legend (blue / amber / red; no colour = no vehicle that minute). */
function SpeedsToggle({ speeds }: { speeds: Props["speeds"] }) {
  if (speeds === null) return null;
  const off = speeds === "none";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, color: palette.ui.textDim, flexWrap: "wrap" }}>
      <label style={{ display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }} title={off ? "No edge stats exported for this scenario yet" : undefined}>
        <input
          type="checkbox"
          checked={!off && speeds.on}
          disabled={off}
          onChange={(e) => !off && speeds.onChange(e.target.checked)}
          style={{ accentColor: palette.ui.accent }}
        />
        Road speeds
      </label>
      <span
        data-testid="speed-legend"
        title="Mean speed over the last minute against the limit; roads without a car that minute stay uncoloured"
        style={{ display: "flex", gap: 8, fontSize: 11, opacity: off || !speeds.on ? 0.4 : 1 }}
      >
        {LEGEND.map((l) => (
          <span key={l.cls} title={l.title} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span style={{ width: 10, height: 3, borderRadius: 2, background: cssRgba(palette.traffic[l.cls]) }} />
            {l.label}
          </span>
        ))}
      </span>
    </div>
  );
}

const button = (active: boolean): CSSProperties => ({
  padding: "6px 12px",
  font: "13px system-ui, sans-serif",
  color: active ? palette.background : palette.ui.text,
  background: active ? palette.ui.accent : "transparent",
  border: `1px solid ${active ? palette.ui.accent : palette.ui.border}`,
  borderRadius: 4,
  cursor: "pointer",
});

export function ControlPanel({ title, activePreset, onPreset, opacity, onOpacity, speeds, status, scenarios }: Props) {
  return (
    <div
      style={{
        position: "absolute",
        left: 12,
        top: 12,
        padding: 12,
        maxWidth: "calc(100% - 24px)",
        background: palette.ui.panel,
        border: `1px solid ${palette.ui.border}`,
        borderRadius: 6,
        color: palette.ui.text,
        font: "13px system-ui, sans-serif",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        zIndex: 5,
      }}
    >
      <h1 style={{ margin: 0, font: "300 16px system-ui, sans-serif", letterSpacing: "0.02em" }}>{title}</h1>
      <div role="group" aria-label="Camera presets" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {CAMERA_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            aria-pressed={activePreset === p.id}
            data-preset={p.id}
            style={button(activePreset === p.id)}
            onClick={() => onPreset(p.id)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, color: palette.ui.textDim }}>
        Buildings
        <input
          type="range"
          aria-label="Building opacity"
          min={BUILDING_OPACITY.min}
          max={BUILDING_OPACITY.max}
          step={0.01}
          value={opacity}
          onChange={(e) => onOpacity(Number(e.target.value))}
          style={{ accentColor: palette.ui.accent, width: 100 }}
        />
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{Math.round(opacity * 100)}%</span>
      </label>
      {scenarios && (
        <div role="group" aria-label="Scenario" style={{ display: "flex", alignItems: "center", gap: 6, color: palette.ui.textDim }}>
          Traffic
          {scenarios.options.map((o) => (
            <button
              key={o.id}
              type="button"
              aria-pressed={scenarios.active === o.id}
              data-scenario={o.id}
              style={button(scenarios.active === o.id)}
              onClick={() => scenarios.onChange(o.id)}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
      <SpeedsToggle speeds={speeds} />
      <div data-testid="status" style={{ color: palette.ui.textDim, fontSize: 12 }}>
        {status}
      </div>
    </div>
  );
}
