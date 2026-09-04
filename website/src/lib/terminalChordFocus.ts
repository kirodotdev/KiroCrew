import { isBottomTerminalOpen, toggleBottomTerminal } from '../hooks/useBottomTerminal'

/**
 * The KEYBOARD path for toggling the docked terminal — the nav rail's button
 * still calls `toggleBottomTerminal` directly, and should.
 *
 * The difference is where focus is when the panel closes. Clicking the rail
 * button leaves focus on that button, which survives the close. The chord's
 * headline use is the opposite case: the skip-shell listener exists precisely so
 * the chord fires while xterm's textarea has focus, and closing then unmounts the
 * subtree that focus was inside (`{open && …}` in `BottomTerminalPanel`), leaving
 * focus on `<body>` — a keyboard that does nothing until the user reaches for the
 * mouse, in a feature whose whole point is not having to.
 *
 * So the chord remembers where it came from when it OPENS the panel and puts
 * focus back when it CLOSES it. The remembered element is used only if it is
 * still in the document; otherwise focus goes to `#main-content`, the app's own
 * "Skip to content" landing zone (`tabIndex={-1}`, so programmatically focusable).
 * A remembered node can legitimately be gone — the user may have navigated, or
 * closed the session whose composer they were typing in — and refocusing nothing
 * would leave exactly the dead keyboard this is meant to prevent.
 *
 * Focus is restored on the next frame, after React has processed the unmount.
 * Setting it synchronously first would work in principle (we move focus OUT of
 * the doomed subtree before it is removed), but ordering against React's commit
 * is not ours to rely on, and a frame's delay is imperceptible.
 */

/** Where focus was when the chord opened the panel. Module scope, not React
 *  state: it is read once, in an event handler, and must not cause a render. */
let openedFrom: Element | null = null

/** True when `el` is inside an xterm instance — the same predicate shape the
 *  keydown seams use to decide whether a keystroke belongs to a shell. */
function isInsideTerminal(el: Element | null): boolean {
  return !!el?.closest?.('.xterm')
}

export function toggleTerminalByChord(cwd?: string): void {
  const wasOpen = isBottomTerminalOpen()

  if (!wasOpen) {
    openedFrom = document.activeElement
    toggleBottomTerminal(cwd)
    return
  }

  // Read focus BEFORE the toggle: after it, the element is on its way out.
  const cameFromShell = isInsideTerminal(document.activeElement)
  toggleBottomTerminal(cwd)

  // Closing with focus already outside the panel (the user clicked back into the
  // composer, say) leaves focus where they put it — nothing to restore.
  if (!cameFromShell) { openedFrom = null; return }

  const remembered = openedFrom
  openedFrom = null
  const target = remembered?.isConnected ? remembered : document.getElementById('main-content')
  const focusTarget = target as HTMLElement | null
  if (!focusTarget) return
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => focusTarget.focus())
  else focusTarget.focus()
}

/** Test-only: clear the remembered element between cases. */
export function __resetTerminalChordFocus(): void { openedFrom = null }
