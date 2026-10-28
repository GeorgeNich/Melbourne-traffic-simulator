import { palette } from "../theme/palette";

/** Shown whenever any loaded data is a hand-made test fixture (buildings and/or trips). Loud on purpose. */
export function FixtureBanner({ buildings, trips }: { buildings: boolean; trips: boolean }) {
  const notes = [buildings && "hand-made rectangles, not real buildings", trips && "hand-made cars, not simulated traffic"].filter(Boolean);
  return (
    <div
      role="alert"
      data-testid="fixture-banner"
      style={{
        position: "absolute",
        top: 12,
        left: "50%",
        transform: "translateX(-50%)",
        maxWidth: "calc(100% - 32px)",
        padding: "8px 16px",
        background: palette.ui.warningBg,
        color: palette.ui.warningText,
        border: `1px solid ${palette.ui.warningText}`,
        borderRadius: 4,
        font: "600 13px system-ui, sans-serif",
        letterSpacing: "0.04em",
        textAlign: "center",
        pointerEvents: "none",
        zIndex: 10,
      }}
    >
      TEST FIXTURE DATA
      <span style={{ fontWeight: 400, letterSpacing: 0 }}> · {notes.join(" · ")}</span>
    </div>
  );
}
