import { useDimInactivePanes } from '../hooks/useDimInactivePanes'

/** Dims an unfocused split-view pane the way Ghostty dims an unfocused split:
 *  a background-coloured rectangle at partial opacity laid over the pane, not
 *  a recolouring of its content. Message text, code highlighting and status
 *  colours keep their own values underneath; only the whole pane reads as
 *  "not the one with focus". Always mounted while the pane knows its focus
 *  state so the change fades both ways; `pointer-events: none` lets the click
 *  that claims focus land on the pane itself. Strength is the
 *  `--pane-dim-opacity` token (index.css). The "Dim inactive panes" chat
 *  setting turns the overlay off for every pane. */
export default function PaneDim({ dimmed }: { dimmed: boolean }) {
  const enabled = useDimInactivePanes()
  const on = dimmed && enabled
  return (
    <div
      aria-hidden
      data-pane-dim={on ? 'on' : 'off'}
      className="pointer-events-none absolute inset-0 z-20 bg-bg transition-opacity duration-150"
      style={{ opacity: on ? 'var(--pane-dim-opacity)' : 0 }}
    />
  )
}
