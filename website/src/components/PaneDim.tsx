/** Dims an unfocused split-view pane the way Ghostty dims an unfocused split:
 *  a background-coloured rectangle at partial opacity laid over the pane, not
 *  a recolouring of its content. Message text, code highlighting and status
 *  colours keep their own values underneath; only the whole pane reads as
 *  "not the one with focus". Always mounted while the pane knows its focus
 *  state so the change fades both ways; `pointer-events: none` lets the click
 *  that claims focus land on the pane itself. Strength is the
 *  `--pane-dim-opacity` token (index.css). */
export default function PaneDim({ dimmed }: { dimmed: boolean }) {
  return (
    <div
      aria-hidden
      data-pane-dim={dimmed ? 'on' : 'off'}
      className="pointer-events-none absolute inset-0 z-20 bg-bg transition-opacity duration-150"
      style={{ opacity: dimmed ? 'var(--pane-dim-opacity)' : 0 }}
    />
  )
}
