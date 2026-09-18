import { useCallback, useEffect, useRef } from 'react'

/**
 * Row-anchored scroll memory for the ChatSidebar session lane.
 *
 * Why this exists: collapsing the sessions sidebar (desktop toggle) or closing
 * the mobile drawer UNMOUNTS ChatSidebar — OverlayDrawer gates its children on
 * `open` — so the lane came back at the top on every reopen.
 *
 * Why not `useScrollMemory` (pixel offset): session rows are
 * `content-visibility: auto` with `contain-intrinsic-size: auto 60px`. On a
 * fresh mount every never-rendered row is laid out at the 60px placeholder
 * while rows the browser has painted are their real height (48px for a
 * one-line title), so the lane's coordinate space is not the same one the
 * user scrolled in: the same `scrollTop` lands on a different session, and it
 * keeps drifting as rows above the viewport get rendered and settle. The
 * stable identity is the ROW — remember which session sat at the top of the
 * lane and how far it was from the lane's edge, and on remount scroll so that
 * row lands there again, correcting over a few frames as placeholder heights
 * resolve.
 *
 * In-memory only (module scope, no localStorage): a reload starts at the top,
 * in step with the side-panel document tabs.
 */

type Anchor = {
  /** `data-session-row` identity of the first row visible at the lane's top. */
  row: string
  /** Distance from the lane's top edge to that row's top edge (px, <= 0 when
   * the row is partially scrolled out). */
  offset: number
  /** Pixel fallback for when the anchor row no longer exists (closed session). */
  scrollTop: number
}

const anchors = new Map<string, Anchor>()

/** Input that means the user has taken over the lane; ends a running restore. */
const USER_INTENT = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const

/** Frames the restore keeps correcting for. Placeholder rows resolve to their
 * real height as they enter the viewport's vicinity, each time moving the
 * anchor row; a handful of frames is enough for the layout to settle. */
const SETTLE_FRAMES = 12

/** Test-only: reset the module-scope store between cases. */
export function _resetLaneScrollMemory(): void {
  anchors.clear()
}

/** Test-only: read what the lane recorded. */
export function _laneScrollAnchor(key: string): Anchor | undefined {
  return anchors.get(key)
}

function measureAnchor(el: HTMLElement): Anchor | null {
  const laneTop = el.getBoundingClientRect().top
  const rows = el.querySelectorAll<HTMLElement>('[data-session-row]')
  for (const row of rows) {
    const r = row.getBoundingClientRect()
    if (r.bottom > laneTop + 1) {
      const id = row.getAttribute('data-session-row')
      if (!id) break
      return { row: id, offset: r.top - laneTop, scrollTop: el.scrollTop }
    }
  }
  return el.scrollTop > 0 ? { row: '', offset: 0, scrollTop: el.scrollTop } : null
}

/** Scroll `el` so the anchor row's top sits `offset` below the lane's top.
 * Returns the residual error in px, or null when the row is gone. */
function applyAnchor(el: HTMLElement, anchor: Anchor): number | null {
  if (!anchor.row) { el.scrollTop = anchor.scrollTop; return 0 }
  const row = el.querySelector<HTMLElement>(`[data-session-row="${window.CSS.escape(anchor.row)}"]`)
  if (!row) return null
  const delta = row.getBoundingClientRect().top - (el.getBoundingClientRect().top + anchor.offset)
  if (Math.abs(delta) >= 0.5) el.scrollTop += delta
  return delta
}

/**
 * @param key  Stable identity of the lane across remounts (one per lane kind).
 *             `null` disables the hook (the board renders its own scrollers).
 * @param ref  The lane element — the same ref the caller attaches.
 * @returns    `onScroll` for the lane element.
 *
 * Restore is one-shot per mount (re-armed when `key` changes, i.e. a flat/tree
 * switch): it runs after commit, then corrects on the next few animation
 * frames while placeholder rows resolve. Any user input on the lane — wheel,
 * touch, pointer, keyboard — ends the correction immediately so a restore can
 * never fight a scroll the user has started. Recording is rAF-coalesced: a
 * scroll storm measures once per frame.
 */
export function useLaneScrollMemory(
  key: string | null,
  ref: React.RefObject<HTMLElement | null>,
): { onScroll: React.UIEventHandler<HTMLElement> } {
  const measureFrame = useRef<number | null>(null)
  const settling = useRef(false)

  useEffect(() => {
    const el = ref.current
    if (!key || !el) return
    const saved = anchors.get(key)
    if (!saved) return
    let frames = 0
    let raf: number | null = null
    settling.current = true
    const stop = () => {
      settling.current = false
      if (raf != null) cancelAnimationFrame(raf)
      raf = null
      for (const t of USER_INTENT) el.removeEventListener(t, stop)
    }
    const step = () => {
      raf = null
      const residual = applyAnchor(el, saved)
      // Anchor row gone (session closed while the sidebar was collapsed):
      // fall back to the pixel offset once and stop correcting.
      if (residual === null) { el.scrollTop = saved.scrollTop; stop(); return }
      if (++frames >= SETTLE_FRAMES) { stop(); return }
      raf = requestAnimationFrame(step)
    }
    for (const t of USER_INTENT) el.addEventListener(t, stop, { passive: true })
    step()
    return stop
  }, [key, ref])

  const onScroll = useCallback<React.UIEventHandler<HTMLElement>>(e => {
    if (!key) return
    const el = e.currentTarget
    if (measureFrame.current != null) return
    measureFrame.current = requestAnimationFrame(() => {
      measureFrame.current = null
      // A scroll event the restore itself produced must not overwrite the
      // anchor with a half-settled position.
      if (settling.current) return
      const a = measureAnchor(el)
      if (a) anchors.set(key, a)
      else anchors.delete(key)
    })
  }, [key])

  useEffect(() => () => {
    if (measureFrame.current != null) cancelAnimationFrame(measureFrame.current)
  }, [])

  return { onScroll }
}
