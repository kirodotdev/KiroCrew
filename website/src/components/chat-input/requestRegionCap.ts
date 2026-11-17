import { useLayoutEffect, useState } from 'react'
import { CLEARANCE_GAP_PX, overlayBottom, topOverlays } from '../../pages/chat/browserLiveClearance'

/* The approval card sits in a dock anchored to the chat pane's bottom and grows
   upward. Uncapped, a tall request on a short pane (a phone keyboard, a small
   window, a long draft) pushes the card's top past the pane's clipped top or
   under the title overlay, and a person could press Allow once with part of the
   command out of sight. So the card's request region (the kind chips, the exact
   command, the call's arguments) is one scroller whose height is capped at what
   keeps the whole card below that line; the region cues whatever it then hides.

   The cap is `min(40svh, measured)`. 40svh leaves the composer, its draft and
   the transcript's last turn in view on an ordinary window. The measured value
   is the room between the region's bottom and the higher of the title
   overlay's lower edge (plus the clearance gap; the card's own split-view pane's
   overlay, never another pane's) and the top of every clipping ancestor, less
   the card's own heading above the region. */

/** The fewest pixels the region keeps, so a few lines and its cue stay legible. */
export const REQUEST_REGION_MIN_PX = 64

/** Where the card's overlays live: its own split-view pane, or the page when
 *  it has none. Another pane's pinned prompt never covers this card. */
function overlayScope(card: HTMLElement): ParentNode {
  return card.closest('[data-chat-pane]') ?? document
}

/** The card's ancestors that clip it. */
function clippingAncestors(card: HTMLElement): HTMLElement[] {
  const out: HTMLElement[] = []
  for (let p = card.parentElement; p; p = p.parentElement) {
    const { overflowY } = getComputedStyle(p)
    if (overflowY === 'hidden' || overflowY === 'clip' || overflowY === 'auto' || overflowY === 'scroll') out.push(p)
  }
  return out
}

/** A split-view pane's own band over its top: its title row, which paints over
 *  the dock, and a host band floating there (`topInset`, the Members DM
 *  header). These are what the page's title overlay is to the single chat. */
function paneBands(card: HTMLElement): HTMLElement[] {
  const pane = card.closest<HTMLElement>('[data-chat-pane]')
  const title = pane?.querySelector<HTMLElement>('[data-pane-title-row]')
  return title ? [title] : []
}

/** The lowest edge of those bands on screen, or null when the pane has none. */
function paneBandBottom(card: HTMLElement): number | null {
  const pane = card.closest<HTMLElement>('[data-chat-pane]')
  if (!pane) return null
  let bottom = -Infinity
  const inset = Number(pane.dataset.paneTopInset ?? 0)
  if (inset > 0) bottom = pane.getBoundingClientRect().top + inset
  for (const el of paneBands(card)) bottom = Math.max(bottom, el.getBoundingClientRect().bottom)
  return Number.isFinite(bottom) ? bottom : null
}

/** The highest on-screen line the card's top may reach. */
export function topBound(card: HTMLElement): number {
  let bound = 0
  // Never the card's own ancestors: a dock marked as a top overlay (a top-down
  // layout) would otherwise bound the card it holds and keep it cramped.
  const overlay = overlayBottom(overlayScope(card), card)
  if (overlay !== null) bound = Math.max(bound, overlay + CLEARANCE_GAP_PX)
  const band = paneBandBottom(card)
  if (band !== null) bound = Math.max(bound, band + CLEARANCE_GAP_PX)
  for (const p of clippingAncestors(card)) bound = Math.max(bound, p.getBoundingClientRect().top)
  // iOS Safari pans the page under an open keyboard: what is on screen starts
  // at the visual viewport's top, not the page's (see useKeyboardLift).
  return Math.max(bound, window.visualViewport?.offsetTop ?? 0)
}

/** The room in px the region has before the floor, given the card and the
 *  region inside it. Below `REQUEST_REGION_MIN_PX` the region still takes the
 *  floor, so the card's top goes past the line: the card is then cramped. */
export function requestRegionRoom(card: HTMLElement, region: HTMLElement): number {
  const c = card.getBoundingClientRect()
  const r = region.getBoundingClientRect()
  const heading = Math.max(0, r.top - c.top)
  return Math.floor(r.bottom - topBound(card) - heading)
}

/** The region's cap in px, given the card and the region inside it. */
export function requestRegionCap(card: HTMLElement, region: HTMLElement): number {
  return Math.max(REQUEST_REGION_MIN_PX, requestRegionRoom(card, region))
}

/** The cap, and whether the room fell below the floor. A cramped card shows
 *  part of its request past the pane's top or under the title overlay, with no
 *  scroll edge to cue it, so it holds its approving controls until the room
 *  returns (ToolApprovalCard). */
export interface RegionCap { cap: number; cramped: boolean }

/** Keeps the cap current as the dock, the card, a pane that clips it, its
 *  overlays or the window resizes, and on any scroll. */
export function useRequestRegionCap(card: HTMLElement | null, region: HTMLElement | null): RegionCap | null {
  const [cap, setCap] = useState<RegionCap | null>(null)
  useLayoutEffect(() => {
    if (!card || !region) { setCap(null); return }
    const measure = () => {
      // A card with no box (not laid out, or hidden) shows nothing to cut.
      if (card.getBoundingClientRect().height === 0) { setCap(null); return }
      const room = requestRegionRoom(card, region)
      // The region draws at min(its content, the cap), so the card's top
      // passes the line only when that is more than the room. A short request
      // under the floor fits wherever its own height does.
      const next = { cap: Math.max(REQUEST_REGION_MIN_PX, room), cramped: room < Math.min(REQUEST_REGION_MIN_PX, region.scrollHeight) }
      setCap(prev => (prev !== null && prev.cramped === next.cramped && Math.abs(prev.cap - next.cap) < 1 ? prev : next))
    }
    measure()
    const dock = card.closest('[data-testid="composer-dock-root"]') ?? card.parentElement
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    ro?.observe(card)
    if (dock) ro?.observe(dock)
    // The title row and the pinned-prompt banner set the line the card must
    // stay below; either can grow, and the banner comes and goes as the
    // transcript scrolls, so both their size and any scroll re-measure.
    for (const el of [...topOverlays(overlayScope(card)), ...paneBands(card)]) ro?.observe(el)
    // The panes that clip the card: a split-view divider resizes one without
    // moving the card's own box or firing a window resize.
    for (const el of clippingAncestors(card)) ro?.observe(el)
    window.addEventListener('resize', measure)
    document.addEventListener('scroll', measure, { capture: true, passive: true })
    const vv = window.visualViewport
    vv?.addEventListener('resize', measure)
    vv?.addEventListener('scroll', measure)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', measure)
      document.removeEventListener('scroll', measure, { capture: true })
      vv?.removeEventListener('resize', measure)
      vv?.removeEventListener('scroll', measure)
    }
  }, [card, region])
  return cap
}
