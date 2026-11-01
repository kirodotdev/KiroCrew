/** The hover hold: keeps the row under the pointer in place while the list re-sorts
 *  under it. The seat arithmetic lives in ../chat/hoverHold. */
import { useRef, useReducer, useCallback, useEffect, type MutableRefObject } from 'react'
import { type HoverPin, heldSeat, seatInSection } from '../chat/hoverHold'
import { sessionRowsInScope, SESSION_ROW_SELECTOR } from '../chat/sessionRowNav'
import type { Slot } from './types'
import { sessionRowIdentity } from './rowIdentity'
import type { TagColumn } from '../../types'

/** Date-segment header between rows. Marks the geometry a row's own rect cannot
 *  see, so the hover hold can anchor on a pixel offset headers contribute to. */
const DATE_HEADER_SELECTOR = '[data-date-header]'
/** A "Group by kind" section header (./kindSections). The hold never seats a row
 *  across one (see `sectionSeat`), so only the date headers inside the row's own
 *  section enter its anchor. */
const KIND_HEADER_SELECTOR = '[data-kind-section]'

/** Marks a dormant-collapse region, so the hold can tell which side of the
 *  expander the pointer found a row on. */
const STALE_REGION_SELECTOR = '[data-stale-region]'
// One rendered lane container. Narrower than data-session-scope, which a folder
// tree shares across every folder body and the root rows (see ../chat/hoverHold).
const SESSION_CONTAINER_SELECTOR = '[data-session-container]'

/** The held-row pin, its release, and the per-lane hold applied at render.
 *  `isPinned` names the lane's leading section, the pinned rows the lane order
 *  puts first; null while a search ranks the lane, which has no such section.
 *  The hold never seats a row across that boundary (see `seatInSection`). */
export function useHoverHold({ isPinned }: { isPinned: ((s: Slot) => boolean) | null }) {
  // Hold the row under the pointer in place. Under a last-activity sort,
  // background agent events (touchSlotActivity recency bumps) re-sort the list
  // at any moment, so a row can move out from under the cursor between the user
  // reading it and pressing — the click then lands on whatever row REPLACED it.
  // The close button makes that expensive: the mis-click closes a session.
  //
  // This holds ONE row rather than freezing the list (the drag freeze on `filteredSlots`) for
  // two reasons. Everything else keeps sorting live, so a long hover never
  // leaves a stale list — only the hovered row is out of place, and only by its
  // own displacement. And on release just that row animates to its true index,
  // where a whole-list thaw moves every row at once, including the one the
  // cursor is now travelling toward.
  //
  // Scoped by (key, lane) because a multi-tag session renders in EVERY matching
  // board column: keyed on the slot alone, hovering one column's copy would
  // hold the row in all of them. `data-session-scope` is the lane identity the
  // rows already stamp, which is also what the arrow rove is scoped to.
  // `seenOrder` is the lane's keys as the POINTER FOUND THEM, so the held slot is
  // derived from row identities rather than a number later rows can shift under.
  // In a REF, not state: hover itself is pure CSS, so arming the hold must not
  // commit the whole sidebar on every row boundary the cursor crosses.
  // headerPxAbove/headerH carry DATE-HEADER geometry: a lane that renders segment
  // headers moves the row when one collapses, so row heights alone under-measure it.
  // staleSide is which side of the DORMANT expander the pointer found the row on,
  // frozen so a bump cannot carry it across into a lane the anchor cannot address.
  const hoverPinRef = useRef<HoverPin | null>(null)
  // Whether the last render actually MOVED the row. Releasing only needs a commit
  // in that case, and the flat lane's header rule reads this same flag.
  const heldDisplacedRef = useRef(false)
  // "Group by kind": each row's section rank as the lanes last drew it, by row
  // identity. Bounded by the sessions the sidebar has drawn.
  const drawnRankRef = useRef(new Map<string, number>())
  const [, bumpHold] = useReducer((c: number) => c + 1, 0)

  const releaseHoverPin = useCallback(() => {
    if (!hoverPinRef.current) return
    hoverPinRef.current = null
    // Commit whenever a pin existed: heldDisplacedRef is written during render and
    // read here from an event, so a discarded or concurrent render desyncs it.
    heldDisplacedRef.current = false
    bumpHold()
  }, [])

  const holdHovered = (list: Slot[], scope: string, container: string, segmentOf?: (s: Slot) => string, sectionRank?: (s: Slot) => number): Slot[] => {
    const pin = hoverPinRef.current
    // Remember each row's section as last drawn, so a captured row that has since
    // left the list still counts toward its own section only (see `sectionSeat`).
    if (sectionRank) for (const s of list) drawnRankRef.current.set(sessionRowIdentity(s), sectionRank(s))
    // Container, not just scope: sibling containers share a nav lane, so a scope-only
    // match would seat the row against a frame spanning rows this list never renders.
    if (!pin || pin.scope !== scope || pin.container !== container) return list
    // `pin.key` is origin-qualified (from `data-session-row`), so match each
    // slot through `sessionRowIdentity`; a raw `s.key` compare would drop a
    // colliding peer AND local row together and reseat the wrong one.
    const at = list.findIndex(s => sessionRowIdentity(s) === pin.key)
    // -1 is the normal case for every lane that does not contain the hovered
    // row, including a sibling list sharing this scope, so it is not an error.
    if (at < 0) return list
    const seat = sectionRank ? null : heldSeat(pin, list, sessionRowIdentity, segmentOf)
    const rest = list.filter(s => sessionRowIdentity(s) !== pin.key)
    // Inside its own section only: across the pinned boundary the lane would
    // read pinned, unpinned, pinned, and draw the pinned divider twice.
    // "Group by kind" sections (the rank orders `list`, pinned first) are seated
    // inside the held row's own section, measured from that section's top, so no
    // section header draws twice and none skews the pixel anchor.
    const held = sectionRank
      ? sectionSeat(pin, list, sessionRowIdentity, sectionRank, segmentOf, k => drawnRankRef.current.get(k))
      : seat == null || !isPinned ? seat : seatInSection(seat, rest, isPinned(list[at]), isPinned)
    if (held == null || held === at) { heldDisplacedRef.current = false; return list }
    heldDisplacedRef.current = true
    // Clamp: the list can shrink under the hold (a session closes, a filter
    // narrows), and splice past the end would silently append instead.
    rest.splice(Math.min(held, rest.length), 0, list[at])
    return rest
  }

  // The ONE place a lane's hold identity is named: holdHovered's scope and the
  // navScope the rows stamp must match, and so must the container, so all come from here.
  const heldLane = (list: Slot[], navScope: string, container: string, segmentOf?: (s: Slot) => string, sectionRank?: (s: Slot) => number) =>
    ({ rows: holdHovered(list, navScope, container, segmentOf, sectionRank), navScope, container })

  // Delegated on the sidebar root so the rows stay memo-clean (a per-row
  // handler prop would be a new identity every render). pointerover fires on
  // entering any descendant, so this covers row→row travel, row→chrome, and
  // row→gap in one handler; pointerleave on the root is the exit backstop.
  const onRootPointerOver = useCallback((e: React.PointerEvent) => {
    // Hovering pointers only: a pen hovers and so reveals the same group-hover
    // action bar (Close included), while a touch tap has no hover state to protect.
    if (e.pointerType !== 'mouse' && e.pointerType !== 'pen') return
    const row = ((e.target as HTMLElement | null)?.closest?.('[data-session-row]') ?? null) as HTMLElement | null
    const key = row?.getAttribute('data-session-row') || ''
    if (!row || !key) { releaseHoverPin(); return }
    const scope = row.getAttribute('data-session-scope') || 'list'
    const prev = hoverPinRef.current
    if (prev && prev.key === key && prev.scope === scope) return
    // Order AND heights read HERE, from the committed DOM the pointer arrived over.
    // A ref write is synchronous, so no re-sort can hand us a post-sort frame.
    // Confined to the row's own CONTAINER: sibling containers share the nav scope, and
    // counting their rows would overshoot the height of the list this row renders in.
    const container = row.closest<HTMLElement>(SESSION_CONTAINER_SELECTOR)?.dataset.sessionContainer ?? ''
    const seenOrder: string[] = []
    const heights: Record<string, number> = {}
    for (const el of sessionRowsInScope(row)) {
      const k = el.getAttribute('data-session-row') || ''
      if (!k) continue
      if ((el.closest<HTMLElement>(SESSION_CONTAINER_SELECTOR)?.dataset.sessionContainer ?? '') !== container) continue
      seenOrder.push(k)
      heights[k] = el.getBoundingClientRect().height
    }
    // Headers are the rows' siblings in the lane, but a row sits inside its own
    // menu wrappers, so climb to the nearest ancestor that actually holds them.
    let headerEls: HTMLElement[] = []
    for (let el = row.parentElement, hop = 0; el && hop < 6 && headerEls.length === 0; el = el.parentElement, hop++) {
      headerEls = Array.from(el.querySelectorAll<HTMLElement>(`${DATE_HEADER_SELECTOR}, ${KIND_HEADER_SELECTOR}`))
    }
    let headerPxAbove = 0
    let sectionHeaderPxAbove = 0
    let headerH = 0
    // Document order, so a section header above the row restarts the in-section sum.
    for (const h of headerEls) {
      const above = !!(h.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING)
      if (h.matches(KIND_HEADER_SELECTOR)) { if (above) sectionHeaderPxAbove = 0; continue }
      const hh = h.getBoundingClientRect().height
      if (hh > headerH) headerH = hh
      if (above) { headerPxAbove += hh; sectionHeaderPxAbove += hh }
    }
    hoverPinRef.current = { key, scope, container, seenOrder, heights, headerPxAbove, sectionHeaderPxAbove, headerH, staleSide: !!row.closest(STALE_REGION_SELECTOR) }
  }, [releaseHoverPin])

  // Two releases pointerleave cannot cover: the window losing focus over a row, and
  // the row leaving the RENDERED set — a filter hides it while it is still in slots.
  useEffect(() => {
    window.addEventListener('blur', releaseHoverPin)
    return () => window.removeEventListener('blur', releaseHoverPin)
  }, [releaseHoverPin])
  return { hoverPinRef, heldDisplacedRef, releaseHoverPin, heldLane, onRootPointerOver }
}

/** Releases the hover hold once its row leaves the rendered scope. */
export function useHoverPinLiveness({ hoverPinRef, releaseHoverPin, filteredSlots, boardLaneActive, flatLaneActive, conductorLaneActive, orderedColumns }: {
  hoverPinRef: MutableRefObject<HoverPin | null>
  releaseHoverPin: () => void
  filteredSlots: Slot[]
  boardLaneActive: boolean
  flatLaneActive: boolean
  conductorLaneActive: boolean
  orderedColumns: TagColumn[]
}) {
  // A pin survives only while its row is still rendered IN THE PINNED SCOPE. Slot
  // membership is key-only, so a lane switch unmounts the scope with the key intact.
  useEffect(() => {
    const pin = hoverPinRef.current
    if (!pin) return
    const live = Array.from(document.querySelectorAll<HTMLElement>(SESSION_ROW_SELECTOR)).some(el =>
      el.dataset.sessionRow === pin.key
      && (el.dataset.sessionScope ?? '') === pin.scope
      && el.closest('[inert]') === null)
    if (!live) releaseHoverPin()
  }, [filteredSlots, boardLaneActive, flatLaneActive, conductorLaneActive, orderedColumns, releaseHoverPin, hoverPinRef])
}

/**
 * The held row's seat in `list` (held row removed) under "Group by kind", or null
 * when the pin says nothing about this list.
 *
 * The row may move only inside its own section, the run of rows sharing its
 * `rankOf`. So the pixel walk (`heldSeat`) runs over that section alone, anchored
 * on the captured rows of the same section and the date headers between the
 * section's top and the row (`sectionHeaderPxAbove`). Section headers then never
 * enter either side of the comparison, and the result is offset by where the
 * section starts in `list`.
 */
export function sectionSeat(
  pin: HoverPin,
  list: readonly Slot[],
  idOf: (s: Slot) => string,
  rankOf: (s: Slot) => number,
  segmentOf?: (s: Slot) => string,
  drawnRankOf: (id: string) => number | undefined = () => undefined,
): number | null {
  const heldRow = list.find(s => idOf(s) === pin.key)
  if (!heldRow) return null
  const rank = rankOf(heldRow)
  const rankById = new Map(list.map(s => [idOf(s), rankOf(s)] as const))
  const section = list.filter(s => rankOf(s) === rank)
  const sectionPin: HoverPin = {
    ...pin,
    // A captured row no longer in the list keeps its place in the anchor, as in
    // `heldSeat`, when it was drawn in this section (`drawnRankOf`); a row known
    // to sit in another section, now or when last drawn, leaves it.
    seenOrder: pin.seenOrder.filter(k => (rankById.get(k) ?? drawnRankOf(k) ?? rank) === rank),
    headerPxAbove: pin.sectionHeaderPxAbove ?? pin.headerPxAbove,
  }
  const local = heldSeat(sectionPin, section, idOf, segmentOf)
  if (local == null) return null
  const rest = list.filter(s => idOf(s) !== pin.key)
  let start = 0
  while (start < rest.length && rankOf(rest[start]) < rank) start++
  return start + local
}
