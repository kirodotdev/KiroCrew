import { useEffect, useRef, useState } from 'react'
import type { Terminal, IMarker } from '@xterm/xterm'
import { logicalLineBounds } from '../utils/terminalLogicalLine'

/**
 * Touch range-selection for the terminal — the piece #6834 adds on top of
 * #6560's staged Select key.
 *
 * WHY THIS EXISTS. xterm's built-in drag-selection is driven by mouse events
 * (mousedown → mousemove → mouseup) and never fires on touch, so CliPanel's
 * mouseup-wired selection toolbar never appears on a phone. #6560 gave touch
 * devices a Copy key plus a *staged* Select key (tap 1 = bottommost output
 * line, tap 2 = whole buffer). What #6560 deliberately left out — and the
 * #6560 Design review asked to track as #6834 — is grabbing an ARBITRARY range
 * out of the MIDDLE of scrollback: "three lines out of the middle of a long
 * scrollback." The staged key can only give you the last line or everything.
 *
 * THE GESTURE (tap-two-endpoints, line granularity). A custom drag-handle UI
 * would mean hand-built DOM overlays positioned over xterm's SCROLLING CANVAS
 * and re-clamped on every scroll/reflow — the fragile parallel-selection path
 * First Principles flagged on #6560's removed "output block" stage. Instead we
 * reuse the machinery #6560 already built:
 *   • A LONG-PRESS on a terminal line sets the ANCHOR endpoint (that row is
 *     highlighted via term.selectLines(row, row)).
 *   • A subsequent TAP (or long-press) on another line sets the FOCUS endpoint;
 *     the selection spans anchor↔focus inclusive, expanded through wrapped
 *     continuation rows exactly the way #6560's Select key expands a logical
 *     line. A second endpoint on the SAME row selects that one line.
 *   • The existing Copy soft key then reads term.getSelection() unchanged — this
 *     hook only CREATES the selection, the same contract the Select key has.
 *
 * The copy unit on a ~40-col phone is a whole (possibly wrapped) LOGICAL LINE,
 * not a character offset, so endpoints are rows: this sidesteps a fiddly
 * per-character drag on a tiny screen while reaching the stated user goal.
 *
 * COMPOSES, doesn't fork. The anchor state lives here; when the selection is
 * cleared through the terminal itself (ESC, a viewport tap that xterm treats as
 * a click, a reflow) xterm fires onSelectionChange with an empty selection and
 * we drop the pending anchor — the same reset discipline #6560's Select stage
 * uses, so the two never disagree about whether a selection is live. A scroll
 * mid-gesture also drops the anchor: the highlighted row would otherwise scroll
 * away under the user and the second tap would land on unrelated content.
 *
 * STALENESS. Every geometry read is gated on term.element?.offsetParent: a tab
 * switch / remote-instance switch / closed pane sets display:none on the host
 * (offsetParent === null), and a long-press timer that fires against a hidden
 * terminal must not mutate a pane the user cannot see — the same gate the async
 * Copy/Paste keys use.
 */

/** Long-press threshold: how long a touch must stay roughly still before it
 *  counts as "set an endpoint here" rather than a tap-scroll. 500ms matches the
 *  platform long-press convention (Android/iOS text selection). */
const LONG_PRESS_MS = 500
/** Movement tolerance during the press. A touch that drifts more than this many
 *  CSS px before the timer fires is a scroll/pan, not a long-press — cancel. */
const MOVE_TOLERANCE_PX = 10

/** The status a caller renders so the range gesture is discoverable (it is
 *  otherwise invisible: nothing on a canvas tells the user an anchor is set). */
export type TouchSelectStatus = 'range_anchor' | 'range_selected' | null

/**
 * Map a client Y coordinate to an ABSOLUTE buffer row (scrollback + viewport),
 * or null when the geometry cannot be read. Deliberately public + pure so the
 * row math is unit-testable without a real canvas: xterm exposes no
 * point→row API, so we derive it from the rendered cell height.
 *
 * `screenRect` is the bounding rect of xterm's `.xterm-screen` element (the
 * grid, excluding the scrollbar). Row height is that rect's height / term.rows;
 * the viewport-relative row is floor((clientY − rect.top) / cellHeight),
 * clamped to [0, rows−1]. Adding buffer.active.viewportY (the scrollback offset
 * of the topmost visible row) yields the absolute buffer index selectLines
 * wants — so a tap while scrolled up into history addresses the line the user
 * is actually looking at.
 */
export function clientYToBufferRow(
  clientY: number,
  screenRect: { top: number; height: number },
  rows: number,
  viewportY: number,
): number | null {
  if (rows <= 0 || screenRect.height <= 0) return null
  const cellHeight = screenRect.height / rows
  if (!(cellHeight > 0)) return null
  const rel = clientY - screenRect.top
  const viewportRow = Math.floor(rel / cellHeight)
  const clamped = Math.max(0, Math.min(viewportRow, rows - 1))
  return viewportY + clamped
}

/**
 * Expand `row` to the bounds of its LOGICAL line and return [top, bottom].
 * Thin wrapper over the shared `logicalLineBounds` walk (utils/terminalLogicalLine)
 * so the touch hook and TerminalKeyBar's Select key cannot diverge on wrap
 * semantics — the DRY concern the #8070 Design + First Principles reviews raised.
 */
function termLogicalLineBounds(term: Terminal, row: number): [number, number] {
  const buffer = term.buffer?.active
  const len = buffer?.length ?? 0
  return logicalLineBounds(row, len, (r) => buffer?.getLine(r)?.isWrapped === true)
}

export interface TerminalTouchSelection {
  /** Bind to the terminal viewport wrapper's onTouchStart. */
  onTouchStart: (e: React.TouchEvent) => void
  /** Bind to onTouchMove — cancels a pending long-press if the finger pans. */
  onTouchMove: (e: React.TouchEvent) => void
  /** Bind to onTouchEnd / onTouchCancel — clears the pending long-press timer
   *  and, on a committing gesture, preventDefaults the synthetic mouse events. */
  onTouchEnd: (e: React.TouchEvent) => void
  /** Discoverability status for the caller's sr-only live region + hint. */
  status: TouchSelectStatus
}

/**
 * Wire touch range-selection onto a terminal. Returns touch handlers to spread
 * on the viewport wrapper plus a `status` for an announcement region.
 *
 * `enabled` is the caller's touch-device gate (useIsTouchDevice) — on a
 * mouse device xterm's own drag-selection already works, so this stays inert.
 */
export function useTerminalTouchSelection(term: Terminal, enabled: boolean): TerminalTouchSelection {
  const [status, setStatus] = useState<TouchSelectStatus>(null)
  // The anchor endpoint (absolute buffer row) once a long-press has set it, or
  // null when no range gesture is in progress. A ref, not state: the second
  // tap's handler must read the latest anchor without depending on a render
  // having flushed.
  const anchorRef = useRef<number | null>(null)
  // An xterm marker pinned to the anchor row. A marker's `.line` is kept
  // current by xterm as the buffer trims (and goes to -1 when the marked line
  // is itself trimmed away), so it distinguishes the two scroll causes exactly:
  // a pure PAN through existing scrollback leaves `.line` equal to the anchor
  // (keep the gesture — that is what makes ranges taller than one viewport
  // selectable, the point of #6834), while NEW output that shifts or trims the
  // buffer moves or disposes it (abandon). This is robust where a {baseY,length}
  // signature is BLIND: at scrollback capacity xterm trims one line per new
  // line, so baseY and length stay constant while every absolute row remaps —
  // the exact long-scrollback case #6834 targets (Design review #8070).
  const anchorMarker = useRef<IMarker | null>(null)
  const disposeAnchorMarker = () => { anchorMarker.current?.dispose?.(); anchorMarker.current = null }
  // Run a selection mutation the hook OWNS with the selfSelecting flag raised,
  // so onSelectionChange (which xterm fires synchronously) attributes the
  // resulting change to us and does not mistake it for a foreign replacement.
  const withSelfSelect = (fn: () => void) => {
    selfSelecting.current = true
    try { fn() } finally { selfSelecting.current = false }
  }
  // Pending long-press: the timer handle plus the start point, so onTouchMove
  // can cancel it if the finger travels too far (a scroll, not a press).
  const pressTimer = useRef<ReturnType<typeof setTimeout>>()
  const pressStart = useRef<{ x: number; y: number; row: number } | null>(null)
  // Set true the moment the long-press timer fires (it committed an endpoint on
  // its own), so onTouchEnd can tell a tap (timer still pending) from the tail
  // of a long-press (timer already fired) — and suppress the synthetic click in
  // both committing cases. Cleared on the next touchstart.
  const longPressFired = useRef(false)
  // Set while the hook is issuing its OWN selection mutation (selectLines /
  // clearSelection), so the onSelectionChange listener can tell a change WE
  // caused from a FOREIGN one — e.g. the Select soft key building its own
  // selection while our anchor is still pending. A foreign non-empty change
  // must abandon our stale anchor + chip, which otherwise linger because
  // getSelection() is not '' (UX review #8070).
  const selfSelecting = useRef(false)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      clearTimeout(pressTimer.current)
      disposeAnchorMarker()
    }
  }, [])

  // Keep the anchor honest about the buffer: when the selection is cleared
  // through the terminal (ESC, a click xterm registers, a reflow) or scrolled
  // away, drop the pending anchor and the status so the NEXT gesture starts
  // fresh. This is the same reset the #6560 Select stage does via
  // onSelectionChange — subscribing here keeps the two consistent.
  useEffect(() => {
    if (!enabled) return
    const clear = () => {
      anchorRef.current = null
      disposeAnchorMarker()
      setStatus(null)
    }
    const selDisp = term.onSelectionChange?.(() => {
      if (!aliveRef.current) return
      if ((term.getSelection?.() ?? '') === '') { clear(); return }
      // Non-empty change we did NOT cause, with our anchor still pending: some
      // other surface (the Select soft key) replaced the selection. Our anchor
      // and chip now describe a selection that no longer exists — abandon them
      // so the next tap starts fresh and the chip stops contradicting the key
      // bar's own stage label (UX review #8070). Our own mutations set
      // selfSelecting, so they don't trip this.
      if (!selfSelecting.current && anchorRef.current !== null) {
        anchorRef.current = null
        disposeAnchorMarker()
        setStatus(null)
      }
    })
    // A scroll fires for BOTH a pure pan through existing scrollback and for new
    // output arriving. Only the latter moves the highlighted anchor row under
    // the user — the anchor marker tells them apart: xterm keeps `marker.line`
    // current through trims, so a pan leaves it equal to the recorded anchor
    // (keep the gesture; that is what makes ranges taller than one viewport
    // selectable, the point of #6834), while new output that shifts/trims moves
    // it or disposes it to -1 (abandon). A pending press (long-press not yet
    // fired) is always cancelled on scroll — a finger that moved enough to
    // scroll is a pan, not a press.
    const scrollDisp = term.onScroll?.(() => {
      if (!aliveRef.current) return
      clearTimeout(pressTimer.current)
      pressStart.current = null
      if (anchorRef.current === null) return
      const marker = anchorMarker.current
      const stillAnchored = marker && !marker.isDisposed && marker.line >= 0 && marker.line === anchorRef.current
      if (stillAnchored) return // pure pan — the marker confirms the anchor row is unchanged
      // The buffer moved under the anchor (shift, or trim at capacity where a
      // {baseY,length} check would be blind): abandon the half-built gesture and
      // clear its now-meaningless lone-line highlight.
      anchorRef.current = null
      disposeAnchorMarker()
      setStatus(null)
      if (term.element?.offsetParent) withSelfSelect(() => term.clearSelection?.())
    })
    return () => { selDisp?.dispose?.(); scrollDisp?.dispose?.() }
  }, [term, enabled])

  /** Read the absolute buffer row under a touch point, or null if geometry is
   *  unreadable / the pane is hidden (staleness gate). */
  const rowAtTouch = (clientY: number): number | null => {
    const el = term.element
    // Staleness gate: a hidden/detached pane has a null offsetParent. Do not
    // measure or mutate a terminal the user cannot see.
    if (!el || !el.offsetParent) return null
    const screen = el.querySelector('.xterm-screen') as HTMLElement | null
    const rect = (screen ?? el).getBoundingClientRect()
    const viewportY = term.buffer?.active?.viewportY ?? 0
    // Line granularity needs only the Y coordinate — the column is irrelevant
    // when the copy unit is a whole logical line.
    return clientYToBufferRow(clientY, { top: rect.top, height: rect.height }, term.rows ?? 0, viewportY)
  }

  /** Commit an endpoint at `row`: first endpoint = anchor (highlight the line),
   *  second endpoint = focus (select the inclusive logical-line span). */
  const commitEndpoint = (row: number) => {
    if (!aliveRef.current || !term.element?.offsetParent) return
    const anchor = anchorRef.current
    if (anchor === null) {
      // First endpoint: highlight just this logical line, remember its row, and
      // pin a marker to it so the scroll handler can tell a pure pan (keep) from
      // new output / a capacity trim (abandon). registerMarker's offset is
      // relative to the cursor row (baseY + cursorY).
      const [top, bottom] = termLogicalLineBounds(term, row)
      withSelfSelect(() => term.selectLines?.(top, bottom))
      anchorRef.current = row
      disposeAnchorMarker()
      const active = term.buffer?.active
      if (active && term.registerMarker) {
        const cursorAbs = (active.baseY ?? 0) + (active.cursorY ?? 0)
        anchorMarker.current = term.registerMarker(row - cursorAbs) ?? null
      }
      setStatus('range_anchor')
      return
    }
    // Second endpoint: span anchor↔focus inclusive, each expanded to its
    // logical-line bounds so a wrapped endpoint contributes its whole line. Use
    // the marker's CURRENT row for the anchor when it survived (a benign shift
    // may have moved it while keeping it valid); fall back to the recorded row.
    const marker = anchorMarker.current
    const anchorRow = marker && !marker.isDisposed && marker.line >= 0 ? marker.line : anchor
    const [aTop, aBottom] = termLogicalLineBounds(term, anchorRow)
    const [fTop, fBottom] = termLogicalLineBounds(term, row)
    const top = Math.min(aTop, fTop)
    const bottom = Math.max(aBottom, fBottom)
    withSelfSelect(() => term.selectLines?.(top, bottom))
    // Range complete: the next long-press starts a fresh gesture.
    anchorRef.current = null
    disposeAnchorMarker()
    setStatus('range_selected')
  }

  /** True when the touch started on the terminal grid (`.xterm-screen`), not on
   *  an overlaid interactive child (Reconnect button, completion menu). Guards
   *  the gesture from stealing those taps. */
  const touchOnTerminalScreen = (e: React.TouchEvent): boolean => {
    const el = term.element
    const screen = el?.querySelector?.('.xterm-screen') ?? null
    const target = e.target as Node | null
    // If we cannot resolve the screen element, fall back to the terminal host so
    // the gesture still works rather than silently never arming.
    const bound = screen ?? el ?? null
    return !!bound && !!target && bound.contains(target)
  }

  const onTouchStart = (e: React.TouchEvent) => {
    if (!enabled) return
    // Multi-touch (pinch-zoom) is never an endpoint gesture.
    if (e.touches.length !== 1) { clearTimeout(pressTimer.current); pressStart.current = null; return }
    // Only engage when the touch is on the terminal grid itself. The handlers
    // sit on a div that also hosts overlaid interactive children — the
    // disconnect banner's Reconnect button and the completion menu — and a
    // committing gesture preventDefaults the tap; without this gate a tap on
    // Reconnect (or a menu row) while an anchor is pending would be swallowed
    // into a selection instead of clicking (Design review #8070). A press that
    // starts off the grid simply never arms the gesture.
    if (!touchOnTerminalScreen(e)) { clearTimeout(pressTimer.current); pressStart.current = null; return }
    const t = e.touches[0]
    const row = rowAtTouch(t.clientY)
    if (row === null) return
    longPressFired.current = false
    pressStart.current = { x: t.clientX, y: t.clientY, row }
    clearTimeout(pressTimer.current)
    pressTimer.current = setTimeout(() => {
      const start = pressStart.current
      pressStart.current = null
      if (!start) return
      longPressFired.current = true
      commitEndpoint(start.row)
    }, LONG_PRESS_MS)
  }

  const onTouchMove = (e: React.TouchEvent) => {
    if (!enabled) return
    const start = pressStart.current
    if (!start) return
    const t = e.touches[0]
    if (!t) return
    // A finger that travels past the tolerance before the timer fires is a
    // scroll/pan, not a long-press — cancel the pending endpoint so scrolling
    // scrollback never accidentally drops an anchor.
    if (Math.abs(t.clientX - start.x) > MOVE_TOLERANCE_PX || Math.abs(t.clientY - start.y) > MOVE_TOLERANCE_PX) {
      clearTimeout(pressTimer.current)
      pressStart.current = null
    }
  }

  const onTouchEnd = (e: React.TouchEvent) => {
    if (!enabled) return
    const start = pressStart.current
    const longFired = longPressFired.current
    clearTimeout(pressTimer.current)
    pressStart.current = null
    longPressFired.current = false

    // Case 1 — tap completes the range: the touch lifted BEFORE the long-press
    // threshold (timer never fired: longFired false) and an anchor is already
    // set. Commit the second endpoint.
    if (start && !longFired && anchorRef.current !== null) {
      commitEndpoint(start.row)
      // CRITICAL: suppress the synthetic mouse events. After a touchend the
      // browser dispatches emulated mousedown/mouseup/click into the same
      // element; xterm binds touchstart as passive (viewport-scroll only) and
      // creates NO selection from touch, but its selection service DOES clear
      // the selection on mousedown (element 'mousedown' -> handleMouseDown ->
      // _handleSingleClick resets selectionStart). That synthetic mousedown
      // would wipe the range we just built, so Copy would then find nothing —
      // the feature's whole flow. preventDefault() on touchend cancels the
      // synthesized mouse sequence (per the touch-events spec), and React binds
      // onTouchEnd non-passively (unlike onTouchStart/onTouchMove), so this
      // takes effect. (Flagged by GPT/Design/UX review on #8070.)
      if (e.cancelable) e.preventDefault()
      return
    }

    // Case 2 — the long-press already fired on the timer (longFired true): it
    // set an anchor (or completed a range). The same synthetic mousedown from
    // THIS lift would clear that fresh selection, so suppress it too.
    if (longFired && e.cancelable) e.preventDefault()
    // Otherwise (no gesture in progress) let the tap fall through to xterm.
  }

  // When disabled (mouse device) or unmounted, never hold an anchor or timer.
  useEffect(() => {
    if (enabled) return
    clearTimeout(pressTimer.current)
    pressStart.current = null
    anchorRef.current = null
    disposeAnchorMarker()
    longPressFired.current = false
    setStatus(null)
  }, [enabled])

  return { onTouchStart, onTouchMove, onTouchEnd, status }
}
