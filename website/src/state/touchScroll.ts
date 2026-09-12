// Pure core of one-finger touch swipe-to-scroll.
//
// A vertical drag maps to one of three transports:
//   - 'wheel'  — mouse tracking is ON (claude, vim `mouse=a`, htop, tmux, fzf):
//     the app scrolls its own pane on mouse-WHEEL reports, which a bare touch
//     never generates, so we synthesize them in the SGR 1006 encoding modern
//     TUIs negotiate.
//   - 'arrows' — alt screen, mouse tracking OFF (less, man, git log): a pager
//     with no scrollback that pages on arrow keys.
//   - 'native' — normal screen, mouse tracking OFF: let the browser pan xterm's
//     scrollback natively (the viewport is overflow-y:scroll).

export type TouchScrollAction = 'native' | 'arrows' | 'wheel'

/** Decide how a one-finger vertical drag should scroll, by mouse mode + screen. */
export function touchScrollAction(
  isAltScreen: boolean,
  mouseTrackingActive: boolean,
): TouchScrollAction {
  // Mouse tracking is checked FIRST, before the buffer type. When it is on,
  // xterm's own touch handler refuses to native-pan the viewport, so 'native'
  // would scroll nothing — the app owns the pointer and a synthesized wheel
  // report is the only thing that scrolls. The buffer type is also not a
  // reliable proxy: after a reconnect/replay the app's mouse mode is re-emitted
  // before the alt-screen enter is re-applied, so the buffer can still read
  // `normal` while the app is really a mouse-tracking full-screen app.
  if (mouseTrackingActive) return 'wheel'
  return isAltScreen ? 'arrows' : 'native'
}

/** Clamp a fling to a sane number of discrete steps (no hundreds-of-keys burst). */
const MAX_STEPS = 6

const enc = (s: string) => new TextEncoder().encode(s)

/**
 * The bytes for `n` scroll steps in a direction, for the 'arrows' or 'wheel'
 * transport. `n` is clamped to [0, MAX_STEPS]; a non-positive count yields no
 * bytes. Not called for 'native' (the browser handles that with no synthesis).
 *
 * `appCursor` is the terminal's DECCKM (application cursor keys) mode. less/vim
 * enable it, and then an arrow key is `ESC O A/B` (SS3), NOT `ESC [ A/B` (CSI) —
 * the CSI form scrolls nothing there. Wheel reports are not cursor keys, so
 * `appCursor` does not affect them.
 *
 * SGR 1006 wheel report: `ESC [ < Cb ; Cx ; Cy M`, wheel-up Cb=64, wheel-down
 * Cb=65, at the 1-based grid cell (`col`;`row`) under the finger. Most TUIs
 * scroll regardless of the cell, but a position-sensitive app — claude's Ink
 * transcript — only scrolls when the report lands inside its scroll box, and
 * the top-left cell `1;1` is a border that scrolls nothing. `col`/`row` default
 * to 1 for callers that do not care; ignored for the 'arrows' transport.
 */
export function scrollSequence(
  transport: 'arrows' | 'wheel',
  direction: 'up' | 'down',
  n: number,
  appCursor = false,
  col = 1,
  row = 1,
): Uint8Array {
  const count = Math.min(Math.max(0, Math.trunc(n)), MAX_STEPS)
  if (count === 0) return new Uint8Array(0)
  let unit: string
  if (transport === 'arrows') {
    const csi = direction === 'up' ? '\x1b[A' : '\x1b[B'
    const ss3 = direction === 'up' ? '\x1bOA' : '\x1bOB'
    unit = appCursor ? ss3 : csi
  } else {
    const cb = direction === 'up' ? 64 : 65
    const cx = Math.max(1, Math.trunc(col))
    const cy = Math.max(1, Math.trunc(row))
    unit = `\x1b[<${cb};${cx};${cy}M`
  }
  return enc(unit.repeat(count))
}

/**
 * Map a touch point (client coords) to the 1-based terminal cell under it, from
 * the terminal's rendered box and grid size. The synthesized wheel report is
 * placed here so a position-sensitive TUI scrolls its own box. Clamped to
 * [1, cols] / [1, rows].
 */
export function touchCellFromRect(
  rect: { left: number; top: number; width: number; height: number },
  cols: number,
  rows: number,
  clientX: number,
  clientY: number,
): [number, number] {
  const cw = rect.width / Math.max(1, cols)
  const ch = rect.height / Math.max(1, rows)
  const col = cw > 0 ? Math.floor((clientX - rect.left) / cw) + 1 : 1
  const row = ch > 0 ? Math.floor((clientY - rect.top) / ch) + 1 : 1
  return [
    Math.min(Math.max(1, col), Math.max(1, cols)),
    Math.min(Math.max(1, row), Math.max(1, rows)),
  ]
}
