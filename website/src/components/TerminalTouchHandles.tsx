import type { HandlePos, TouchSelectionHandles } from '../hooks/useTerminalTouchSelection'
import { TOUCH_HANDLE_ATTR } from '../hooks/useTerminalTouchSelection'
import { i18nT } from '../i18n/t'

/** Hit area around each knob, in px: a fingertip, per the 44pt touch guideline
 *  rounded down to what fits between two adjacent terminal rows. */
const HIT = 36
const KNOB = 12

/** Whether a handle's knob sits above its caret (else below). */
const knobAbove = (which: 'start' | 'end', pos: HandlePos) => (which === 'start') !== !!pos.flip

/**
 * The two drag handles over a touch selection (useTerminalTouchSelection draws
 * nothing itself). Each is a caret bar the height of a cell with a round knob:
 * above the line at the start, below it at the end, the way phone text fields
 * show them. The knob's hit area carries the attribute the hook's listener
 * reads, so dragging is handled there; this component only places them.
 *
 * It also owns the sr-only live region that tells a screen reader a selection
 * was made, the announce-through-a-status-region pattern the key bar's Copy
 * and Select use. The region stays mounted so its text change is announced.
 */
export default function TerminalTouchHandles({ handles }: { handles: TouchSelectionHandles | null }) {
  return (
    <>
      <span role="status" aria-live="polite" className="sr-only">
        {handles ? i18nT('components.cliPanel.touch_select_done') : ''}
      </span>
      {handles && <HandlePair handles={handles} />}
    </>
  )
}

/** A hit area in the host's coordinates. */
type Rect = { left: number; top: number; right: number; bottom: number }

/** The knob's centre: on the caret line, above or below the cell. */
function knobCentre(which: 'start' | 'end', pos: HandlePos): [number, number] {
  return [pos.left, pos.top + (knobAbove(which, pos) ? -KNOB : pos.height) + KNOB / 2]
}

function hitAround([x, y]: [number, number]): Rect {
  return { left: x - HIT / 2, top: y - HIT / 2, right: x + HIT / 2, bottom: y + HIT / 2 }
}

function HandlePair({ handles }: { handles: TouchSelectionHandles }) {
  const { start, end } = handles
  const sc = start && knobCentre('start', start)
  const ec = end && knobCentre('end', end)
  const sHit = sc && hitAround(sc)
  const eHit = ec && hitAround(ec)
  // Two knobs can sit closer than a hit area, on one row or on adjacent rows
  // where an edge row flips one knob toward the other. Split the overlap at
  // the midpoint between the knobs, across the axis they are further apart
  // on, so each knob stays inside its own area.
  if (sc && ec && sHit && eHit && sHit.left < eHit.right && eHit.left < sHit.right
    && sHit.top < eHit.bottom && eHit.top < sHit.bottom) {
    const dx = ec[0] - sc[0]
    const dy = ec[1] - sc[1]
    if (Math.abs(dy) > Math.abs(dx)) {
      const mid = (sc[1] + ec[1]) / 2
      const [above, below] = dy > 0 ? [sHit, eHit] : [eHit, sHit]
      above.bottom = Math.min(above.bottom, mid)
      below.top = Math.max(below.top, mid)
    } else {
      const mid = (sc[0] + ec[0]) / 2
      const [left, right] = dx >= 0 ? [sHit, eHit] : [eHit, sHit]
      left.right = Math.min(left.right, mid)
      right.left = Math.max(right.left, mid)
    }
  }
  return (
    <>
      {start && sHit && <Handle which="start" pos={start} hit={sHit} />}
      {end && eHit && <Handle which="end" pos={end} hit={eHit} />}
    </>
  )
}

function Handle({ which, pos, hit }: { which: 'start' | 'end'; pos: HandlePos; hit: Rect }) {
  const knobTop = knobAbove(which, pos) ? -KNOB : pos.height
  // The caret's box starts at (pos.left - 1, pos.top); the hit area is placed in it.
  const ox = pos.left - 1
  return (
    <div
      className="pointer-events-none absolute z-20"
      style={{ left: ox, top: pos.top, width: 2, height: pos.height }}
      data-testid={`terminal-touch-handle-${which}`}
      aria-hidden="true"
    >
      <div className="absolute inset-0 bg-accent" />
      <div
        className="absolute rounded-full bg-accent"
        style={{ left: 1 - KNOB / 2, top: knobTop, width: KNOB, height: KNOB }}
      />
      <div
        {...{ [TOUCH_HANDLE_ATTR]: which }}
        className="pointer-events-auto absolute"
        style={{
          left: hit.left - ox,
          top: hit.top - pos.top,
          width: Math.max(0, hit.right - hit.left),
          height: Math.max(0, hit.bottom - hit.top),
          touchAction: 'none',
        }}
      />
    </div>
  )
}
