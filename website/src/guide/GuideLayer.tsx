/**
 * The registered-action guide's on-screen layer: one small floating panel and,
 * while a step's target is found, an arrow and outline on that control. It
 * floats over every page for the states that walk the user somewhere (an
 * active step, a missing target, a step the user navigated away from, another
 * tab, re-entry, a refusal); the offer
 * and the result live in the slot's chat (GuideOfferCard). Once the guide this
 * tab drove ends, the same panel becomes a chip saying so with the way back to
 * that chat, shown only while the user is not already on it.
 *
 * The panel never reserves layout space. While a step's target is tracked it
 * is a popover anchored to that control on the side OPPOSITE the arrow
 * (`popoverPlacement`), so the explanation reads next to what it explains; in
 * every other state it is a chip floating at the bottom centre. It is ONE
 * element in both forms, so the eye follows it from the chip to the control.
 *
 * Non-modal by construction. There is no scrim and no full-screen element:
 * the arrow and outline are `pointer-events: none` and hidden from assistive
 * tech, so every click and key still reaches the page underneath, and only the
 * panel's own buttons take input. The panel never covers the target and never
 * takes focus on its own.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import { AlertTriangle, ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Compass, Loader2, X } from 'lucide-react'
import { GUIDE_LAYER_SELECTOR } from './probeRegistry'
import { Btn, IconButton } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { useGuardedLeave } from '../components/NavigationLeaveGuard'
import { membersRosterQuery } from '../api/membersQuery'
import type { MemberRosterRow } from '../api/client'
import { GUIDE_ONLY_WITH_ITEMS } from '../uiLocations/guidePlans.gen'
import { isDisabled } from './liveRegistry'
import { findUiLocation, itemIsNamed, pickAlternatives, pickItems, pickNameOf, type GuideActionRefusal, type GuideStepPlan } from './guideActions'
import { useGuide, useViewedSlot, type GuideView } from './GuideContext'
import { finishedKeyFor } from './GuideOfferCard'
import { clearFrozenPicks, factVerdict, freezePick, isGuideTargetVisible, offStepRoute, pickMade, resolveGuideTarget, resolveStepTarget, useGuideStepTracker, type GuideRect } from './useGuideStepTracker'
import { GUIDE_PREDICATE_TEXT_KEYS, GUIDE_PREDICATE_TICK_MS, GUIDE_SELECTION_TEXT_KEYS, isRuntimePredicate, selectedName, useUnmetPredicates } from './guidePredicates'
import GuideAgentNote from './GuideAgentNote'
import { openDialogsNow, watchConfirmDialog } from './guideConfirmWatch'
import { accessibleName, findCandidates, findPick, searchByName, setFindPick, type FindQuery } from './findByName'
import { isCautionTarget } from './findTargetPolicy'
import { GUIDE_KEEP_CLEAR_ATTR } from './trustRoot'
import { useFindProbe } from './useFindProbe'

const ARROW = 24
const GAP = 6
/** The outline is drawn this far outside the target box. */
const OUTLINE = 4
/** Minimum distance between the panel and every viewport edge. */
export const PANEL_MARGIN = 8
/** How long a just-started guide looks for its first target before showing
 *  the bottom chip instead. */
const FIRST_TARGET_WAIT_MS = 600
/**
 * How long the finish chip stays before it leaves by itself. Its result line
 * is in the chat already, so the chip is a reminder of the way back, not a
 * fixture: it goes after this long (re-armed while focus is in it, so it never
 * vanishes from under a keyboard user), or on the second move to another page
 * after the guide ended, whichever comes first. Its X still closes it at once.
 * It leaves without an exit animation, so reduced motion changes nothing here.
 */
export const GUIDE_FINISHED_DISMISS_MS = 10_000

/** The finished panel's width: room for its title and its Back button on one row. */
const WIDE_PANEL_MAX_WIDTH = 420
export const PANEL_MAX_WIDTH = 320
/** Below this viewport width the panel spans the viewport minus its margins. */
const NARROW = 768

interface Viewport { width: number; height: number }
export interface Box { top: number; left: number; bottom: number; right: number }

/** Which way the arrow points: at the target from above (`down`), below (`up`), its left (`right`) or right (`left`). */
export type ArrowDir = 'down' | 'up' | 'left' | 'right'
export interface ArrowPlace {
  top: number
  left: number
  /** The arrow is below the target, pointing up at it. */
  up: boolean
  dir: ArrowDir
}

/** Smallest distance kept between the arrow and the viewport edge. */
const ARROW_EDGE = 4
/** How far the arrow's nudge animation moves it away from the target. */
const ARROW_TRAVEL = 4

const boxesTouch = (a: Box, b: Box, pad: number) =>
  a.left < b.right + pad && b.left < a.right + pad && a.top < b.bottom + pad && b.top < a.bottom + pad

/**
 * Where the arrow sits for a target. It keeps GAP from the target's OUTLINE
 * (not the bare box) and GAP from every control in `obstacles` -- the page's
 * neighbouring controls, so the arrow never lands on another control's
 * border. Tried in order: above (below first when the viewport has no room
 * above), the other vertical side, either vertical side shifted along the
 * target to a clear column, then beside the target pointing sideways. When
 * nothing is clear it falls back to the first vertical side: the arrow must
 * still say where the target is.
 */
export function arrowPlacement(rect: GuideRect, viewport: Viewport, obstacles: readonly Box[] = []): ArrowPlace {
  const reach = OUTLINE + GAP
  const minLeft = ARROW_EDGE
  const maxLeft = Math.max(ARROW_EDGE, viewport.width - ARROW - ARROW_EDGE)
  const clampX = (x: number) => Math.min(Math.max(x, minLeft), maxLeft)
  const aboveTop = rect.top - reach - ARROW
  const belowTop = rect.top + rect.height + reach
  const roomAbove = aboveTop >= ARROW_EDGE
  const center = clampX(rect.left + rect.width / 2 - ARROW / 2)
  const inView = (top: number, left: number) =>
    top >= ARROW_EDGE && top + ARROW <= viewport.height - ARROW_EDGE && left >= minLeft && left <= maxLeft
  const clear = (top: number, left: number, dir: ArrowDir) => {
    if (!inView(top, left)) return false
    // The nudge animation moves the arrow ARROW_TRAVEL away from the target,
    // so the box it sweeps, not only where it rests, keeps GAP from neighbours.
    const box: Box = {
      top: top - (dir === 'down' ? ARROW_TRAVEL : 0),
      left: left - (dir === 'right' ? ARROW_TRAVEL : 0),
      bottom: top + ARROW + (dir === 'up' ? ARROW_TRAVEL : 0),
      right: left + ARROW + (dir === 'left' ? ARROW_TRAVEL : 0),
    }
    return !obstacles.some(o => boxesTouch(box, o, GAP))
  }
  const vertical: Array<{ top: number; dir: ArrowDir }> = roomAbove
    ? [{ top: aboveTop, dir: 'down' }, { top: belowTop, dir: 'up' }]
    : [{ top: belowTop, dir: 'up' }, { top: aboveTop, dir: 'down' }]
  const place = (top: number, left: number, dir: ArrowDir): ArrowPlace => ({ top, left, up: dir === 'up', dir })

  for (const v of vertical) if (clear(v.top, center, v.dir)) return place(v.top, center, v.dir)
  // Slide along the target's span, nearest to its centre first.
  const from = clampX(rect.left)
  const to = clampX(rect.left + rect.width - ARROW)
  const columns: number[] = []
  for (let d = 4; d <= rect.width; d += 4) {
    for (const x of [center - d, center + d]) if (x >= from && x <= to) columns.push(x)
  }
  for (const v of vertical) for (const x of columns) if (clear(v.top, x, v.dir)) return place(v.top, x, v.dir)
  const sideTop = rect.top + rect.height / 2 - ARROW / 2
  const right = rect.left + rect.width + reach
  const left = rect.left - reach - ARROW
  if (clear(sideTop, right, 'left')) return place(sideTop, right, 'left')
  if (clear(sideTop, left, 'right')) return place(sideTop, left, 'right')
  // Nothing is clear (a row in a dense list: the rail's entries above and
  // below): the first vertical spot, unless a spot beside the target covers
  // less of its neighbours, so the arrow sits on the target's free side (the
  // content beside a rail entry) rather than over the next entry's label.
  const spots = [
    { top: vertical[0].top, left: center, dir: vertical[0].dir },
    { top: sideTop, left: right, dir: 'left' as ArrowDir },
    { top: sideTop, left, dir: 'right' as ArrowDir },
  ].filter(s => inView(s.top, s.left))
  const covered = (s: { top: number; left: number }) => {
    const box: Box = { top: s.top, left: s.left, bottom: s.top + ARROW, right: s.left + ARROW }
    return obstacles.reduce((n, o) => n + overlap(box, o), 0)
  }
  let best = spots[0]
  for (const s of spots) if (covered(s) < covered(best)) best = s
  if (best) return place(best.top, best.left, best.dir)
  const first = vertical[0]
  return place(first.top, center, first.dir)
}

const NO_BOXES: readonly Box[] = []

/** How often the boxes around a tracked target are re-measured. */
export const GUIDE_RELAYOUT_MS = 500

const sameBoxes = (a: readonly Box[], b: readonly Box[]) =>
  a.length === b.length && a.every((x, i) => x.top === b[i].top && x.left === b[i].left && x.bottom === b[i].bottom && x.right === b[i].right)

/** *boxes*, but the previous array when the measurement did not change. */
function useStableBoxes(boxes: readonly Box[]): readonly Box[] {
  const ref = useRef(boxes)
  if (!sameBoxes(ref.current, boxes)) ref.current = boxes
  return ref.current
}

/** How far from the target the arrow can reach: its candidate spots lie within this band. */
export const ARROW_BAND = OUTLINE + 2 * GAP + ARROW

/**
 * The boxes of the page's controls within *band* of *rect* (clipped to
 * *viewport* when given), other than the target itself, its own parts,
 * anything wrapping it, and the guide's own layer. Only the arrow's placement
 * reads them, within ARROW_BAND; the panel avoids just the target, the arrow
 * and the top bar.
 */
export function nearbyControlBoxes(rect: GuideRect, root: ParentNode = document, band = ARROW_BAND, viewport?: Viewport): Box[] {
  const zone: Box = { top: rect.top - band, left: rect.left - band, bottom: rect.top + rect.height + band, right: rect.left + rect.width + band }
  if (viewport) {
    zone.top = Math.max(zone.top, 0); zone.left = Math.max(zone.left, 0)
    zone.bottom = Math.min(zone.bottom, viewport.height); zone.right = Math.min(zone.right, viewport.width)
  }
  const target: Box = { top: rect.top, left: rect.left, bottom: rect.top + rect.height, right: rect.left + rect.width }
  const out: Box[] = []
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], [role="button"], [role="combobox"], [role="tab"], [role="switch"], [role="radio"], [role="checkbox"]'))) {
    if (el.closest('[data-testid="guide-pill"]')) continue
    const r = el.getBoundingClientRect()
    if (r.width <= 0 || r.height <= 0) continue
    const b: Box = { top: r.top, left: r.left, bottom: r.bottom, right: r.right }
    if (!boxesTouch(b, zone, 0)) continue
    const inside = b.top >= target.top - 1 && b.left >= target.left - 1 && b.bottom <= target.bottom + 1 && b.right <= target.right + 1
    const wraps = b.top <= target.top + 1 && b.left <= target.left + 1 && b.bottom >= target.bottom - 1 && b.right >= target.right - 1
    if (inside || wraps) continue
    out.push(b)
  }
  return out
}


/**
 * The page's own chrome the panel must never cover: everything above the
 * main region (the top bar, which on a phone holds the menu button) and any
 * sticky bar pinned to the main region's top (a settings page's back bar).
 */
export function pageChromeBoxes(viewport: Viewport, root: Document = document): Box[] {
  const main = root.getElementById('main-content')
  if (!main) return []
  const m = main.getBoundingClientRect()
  let bottom = m.top
  for (const el of Array.from(main.querySelectorAll<HTMLElement>('.sticky, .fixed'))) {
    const r = el.getBoundingClientRect()
    if (r.height > 0 && Math.abs(r.top - m.top) <= 2 && r.width >= m.width / 2) bottom = Math.max(bottom, r.bottom)
  }
  const out: Box[] = bottom > 0 ? [{ top: 0, left: 0, bottom, right: viewport.width }] : []
  // The page's own header (its title, the line under it and its main button,
  // such as Add Job) is part of what the person reads to find their way, so
  // the panel stays off it too: across the screen on a phone, over the
  // header's own box on a wider one.
  // The shared PageHeader (`data-testid="page-header"`), else a page's own <header>.
  const head = main.querySelector<HTMLElement>('[data-testid="page-header"]') ?? main.querySelector<HTMLElement>('header')
  const hr = head?.getBoundingClientRect()
  if (hr && hr.height > 0 && hr.bottom > 0 && hr.top < viewport.height && hr.height < viewport.height / 3) {
    out.push(viewport.width < NARROW
      ? { top: Math.max(0, hr.top), left: 0, bottom: hr.bottom, right: viewport.width }
      : { top: Math.max(0, hr.top), left: hr.left, bottom: hr.bottom, right: hr.right })
  }
  // The page's own sub-navigation (Customize's tabs, a settings sub-nav): the
  // person reads the entries around the one a step points at.
  for (const el of Array.from(main.querySelectorAll<HTMLElement>('nav, [role="tablist"]'))) {
    const r = el.getBoundingClientRect()
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < viewport.height && r.height < viewport.height * 0.9) {
      out.push({ top: Math.max(0, r.top), left: Math.max(0, r.left), bottom: Math.min(viewport.height, r.bottom), right: Math.min(viewport.width, r.right) })
    }
  }
  // The guide's own card in the chat ("Guide in progress"), which says what the guide is doing.
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('[data-testid="guide-offer-card"]'))) {
    const r = el.getBoundingClientRect()
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < viewport.height) {
      out.push({ top: Math.max(0, r.top), left: Math.max(0, r.left), bottom: Math.min(viewport.height, r.bottom), right: Math.min(viewport.width, r.right) })
    }
  }
  return [...out, ...keepClearBoxes(viewport, root)]
}

/** On a phone, the strip under a step's target kept clear for the field that follows it. */
export const NEXT_FIELD_PX = 56

/** On a phone, the room above a kept-clear region that the end of the reply takes. */
export const KEEP_CLEAR_REPLY_PX = 72

/**
 * The regions marked `data-guide-keep-clear` (the composer), as drawn now;
 * on a phone each also reserves the strip above it, where the reply ends.
 */
export function keepClearBoxes(viewport: Viewport, root: Document = document): Box[] {
  const out: Box[] = []
  for (const el of Array.from(root.querySelectorAll<HTMLElement>(`[${GUIDE_KEEP_CLEAR_ATTR}]`))) {
    if (el.closest('[data-testid="guide-pill"]')) continue
    const r = el.getBoundingClientRect()
    if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= viewport.height) continue
    const lift = viewport.width < NARROW ? KEEP_CLEAR_REPLY_PX : 0
    out.push({ top: Math.max(0, r.top - lift), left: r.left, bottom: r.bottom, right: r.right })
  }
  return out
}

/** A page dialog that is modal right now (never the guide's own layer). */
export function openModalDialog(root: Document = document): Element | null {
  // The topmost one: dialogs portal to the end of the body, so a confirm
  // opened from inside another dialog comes after it and covers it.
  const open = Array.from(root.querySelectorAll('[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]'))
  for (let i = open.length - 1; i >= 0; i--) {
    if (!open[i].closest(GUIDE_LAYER_SELECTOR)) return open[i]
  }
  return null
}

/**
 * Whether an open page modal hides *el*: the target is outside the dialog and
 * what is drawn at its centre is not the target (a menu portaled above the
 * dialog is). The outline and arrow then wait, rather than being drawn over
 * the dialog the person is reading.
 */
export function coveredByModal(el: Element | null, root: Document = document): boolean {
  const modal = openModalDialog(root)
  if (!modal || !el || modal.contains(el)) return false
  const r = el.getBoundingClientRect()
  const hit = typeof root.elementFromPoint === 'function' ? root.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) : null
  return !(hit && el.contains(hit))
}

/**
 * The rects of the other items a select step with no pick name leaves to
 * choose from (`pickAlternatives`), outlined beside the first so the outline
 * marks the choices, never one of them as the one meant.
 */
export function pickAlternativeRects(step: GuideStepPlan, viewport: Viewport): GuideRect[] {
  const t = step.target
  if (t.kind !== 'location' || !t.pickFrom || t.pickName !== undefined || step.complete.kind !== 'select') return []
  const picker = findUiLocation(t.id, isGuideTargetVisible)
  if (!picker) return []
  return pickAlternatives(picker, isGuideTargetVisible, t.pickControl).map((el) => {
    const r = el.getBoundingClientRect()
    return clampToViewport({ top: r.top, left: r.left, width: r.width, height: r.height }, viewport)
  })
}

/**
 * The name of a select step's only choice, when its picker lists exactly one
 * item and the step names none or names that one; null otherwise.
 */
export function onlyPickName(step: GuideStepPlan): string | null {
  const t = step.target
  if (t.kind !== 'location' || !t.pickFrom) return null
  const picker = findUiLocation(t.id, isGuideTargetVisible)
  const items = picker ? pickItems(picker).filter(isGuideTargetVisible) : []
  const name = items.length === 1 ? pickNameOf(items[0])?.trim() : ''
  if (!name) return null
  return t.pickName === undefined || itemIsNamed(items[0], t.pickName) ? name : null
}

/**
 * The "only one in the list" line for an entity whose pick is made by a
 * control on its row rather than by opening it: the words name what the
 * outline is on (the job's checkbox, the app card's ⋯), never "open".
 */
const ONLY_ONE_TEXT_KEYS: Record<string, string> = {
  tickjob: 'components.guideLayer.select_the_only_one_tick',
  app: 'components.guideLayer.select_the_only_one_app',
}

/**
 * The name the page shows for the item a select step's pick names (`命令栏`
 * for a pick of "Command Bar"), when exactly one drawn item is it; null
 * otherwise. The panel says this name, never the one the pick was given as.
 */
export function namedPickShown(step: GuideStepPlan): string | null {
  const t = step.target
  if (t.kind !== 'location' || !t.pickFrom || t.pickName === undefined) return null
  const picker = findUiLocation(t.id, isGuideTargetVisible)
  const named = picker ? pickItems(picker).filter(isGuideTargetVisible).filter(el => itemIsNamed(el, t.pickName!)) : []
  return named.length === 1 ? pickNameOf(named[0])?.trim() || null : null
}

/** Whether a page modal is open, re-read while *enabled* as the document changes. */
function useOpenModal(enabled: boolean): boolean {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    if (!enabled) { setOpen(false); return }
    const read = () => setOpen(!!openModalDialog())
    read()
    const mo = new MutationObserver(read)
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-modal', 'role', 'data-state'] })
    return () => mo.disconnect()
  }, [enabled])
  return open
}

/** The part of *rect* inside the viewport (a table taller than the screen is outlined where it shows). */
export function clampToViewport(rect: GuideRect, viewport: Viewport, margin = 8): GuideRect {
  const top = Math.max(rect.top, margin)
  const left = Math.max(rect.left, margin)
  const bottom = Math.min(rect.top + rect.height, viewport.height - margin)
  const right = Math.min(rect.left + rect.width, viewport.width - margin)
  if (bottom <= top || right <= left) return rect
  return { top, left, width: right - left, height: bottom - top }
}

export function panelWidth(viewportWidth: number): number {
  const room = Math.max(0, viewportWidth - 2 * PANEL_MARGIN)
  return viewportWidth < NARROW ? room : Math.min(PANEL_MAX_WIDTH, room)
}

export interface PanelPlacement {
  top: number
  left: number
  width: number
  /**
   * `opposite`: the side away from the arrow; `beyond-arrow`: past the arrow
   * on its own side; `beside`: left or right of the target; `corner`: a
   * viewport corner.
   */
  side: 'opposite' | 'beyond-arrow' | 'beside' | 'corner'
}

const overlap = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top))

/**
 * Where the step panel floats for a tracked target. Candidates, in order:
 * opposite the arrow (arrow above the target -> panel below it), past the
 * arrow on its own side, beside the target (past a sideways arrow, its side
 * first), then the viewport corners; each
 * vertical candidate is tried centred on the target, then flush with either
 * of its edges.
 *
 * The first candidate that stays inside the viewport by PANEL_MARGIN and
 * overlaps neither the target outline, the arrow nor the page chrome
 * (`reserved`) wins. When none is clear, the one covering the least is taken,
 * the target and the arrow weighing most. Other controls and labels are not
 * avoided on purpose: the panel stays next to the step it describes, and
 * steering around every neighbour pushed it far away. `obstacles` only keeps
 * the ARROW clear of neighbouring controls. The panel stays non-modal either
 * way: it only ever floats over the page.
 */
export function popoverPlacement(
  rect: GuideRect,
  viewport: Viewport,
  height: number,
  obstacles: readonly Box[] = [],
  reserved: readonly Box[] = [],
): PanelPlacement {
  const m = PANEL_MARGIN
  const width = panelWidth(viewport.width)
  const arrow = arrowPlacement(rect, viewport, obstacles)
  const outline: Box = { top: rect.top - OUTLINE, left: rect.left - OUTLINE, bottom: rect.top + rect.height + OUTLINE, right: rect.left + rect.width + OUTLINE }
  const arrowBox: Box = { top: arrow.top, left: arrow.left, bottom: arrow.top + ARROW, right: arrow.left + ARROW }
  const maxLeft = Math.max(m, viewport.width - width - m)
  const maxTop = Math.max(m, viewport.height - height - m)
  const centred = Math.min(Math.max(rect.left + rect.width / 2 - width / 2, m), maxLeft)
  // Above or below, the panel stays beside the target's span: centred on it,
  // else flush with its left or right edge.
  const clampLeft = (x: number) => Math.min(Math.max(x, m), maxLeft)
  const lefts = [centred, clampLeft(rect.left), clampLeft(rect.left + rect.width - width)]
  // On a phone the control a step points at is usually followed by the field
  // it acts on (Check again, then the voice's speed): the strip right under
  // the outline is kept clear too, so the panel goes above when there is room.
  const nextField: Box[] = viewport.width < NARROW
    ? [{ top: outline.bottom, left: 0, bottom: outline.bottom + NEXT_FIELD_PX, right: viewport.width }]
    : []
  const guarded = [outline, arrowBox, ...nextField]

  type Cand = { top: number; left: number; side: PanelPlacement['side'] }
  const cands: Cand[] = []
  const vertical = (top: number, side: Cand['side']) => { for (const left of lefts) cands.push({ top, left, side }) }
  // `arrow.up`: the arrow sits BELOW the target, pointing up at it.
  vertical(arrow.up ? outline.top - GAP - height : outline.bottom + GAP, 'opposite')
  vertical(arrow.up ? arrowBox.bottom + GAP : arrowBox.top - GAP - height, 'beyond-arrow')
  // Beside the target: past the arrow when it points in from that side (a
  // sideways arrow sits between the target and the panel), and the arrow's
  // own side first, since that is where the room is. Each side is tried
  // centred on the target, then flush with its top or bottom edge, before the
  // viewport corners -- a target at the right edge whose arrow points in from
  // the left gets its panel just left of the arrow, not a far corner.
  const clampTop = (y: number) => Math.min(Math.max(y, m), maxTop)
  const tops = [clampTop(rect.top + rect.height / 2 - height / 2), clampTop(outline.top), clampTop(outline.bottom - height)]
  const rightOf = Math.max(outline.right, arrow.dir === 'left' ? arrowBox.right : -Infinity) + GAP
  const leftOf = Math.min(outline.left, arrow.dir === 'right' ? arrowBox.left : Infinity) - GAP - width
  const sides = arrow.dir === 'right' ? [leftOf, rightOf] : [rightOf, leftOf]
  for (const left of sides) for (const top of tops) cands.push({ top, left, side: 'beside' })
  for (const top of [maxTop, m]) for (const left of [maxLeft, m]) cands.push({ top, left, side: 'corner' })

  const boxOf = (c: Cand): Box => ({ top: c.top, left: c.left, bottom: c.top + height, right: c.left + width })
  const inView = (c: Cand) => c.top >= m - 0.5 && c.left >= m - 0.5 && c.top + height <= viewport.height - m + 0.5 && c.left + width <= viewport.width - m + 0.5
  // The panel stays next to the step it describes. It never covers the
  // target or the arrow, and keeps off the page chrome (on a phone the top
  // bar holds the menu button), but it is free to float over other controls
  // and labels: steering around every neighbour pushed it far from the step,
  // which read worse than covering a control the user is not asked to touch.
  const cost = (c: Cand) => {
    const b = boxOf(c)
    return [
      guarded.reduce((n, f) => n + overlap(b, f), 0),
      reserved.reduce((n, f) => n + overlap(b, f), 0),
    ]
  }
  const less = (a: number[], b: number[]) => { for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i]; return false }
  let best: { c: Cand; k: number[] } | null = null
  for (const c of cands) {
    if (!inView(c)) continue
    const k = cost(c)
    if (k.every(v => v === 0)) return { top: c.top, left: c.left, width, side: c.side }
    if (!best || less(k, best.k)) best = { c, k }
  }
  const c = best?.c ?? { top: maxTop, left: maxLeft, side: 'corner' as const }
  return { top: c.top, left: c.left, width, side: c.side }
}

function subscribeViewport(onChange: () => void) {
  window.addEventListener('resize', onChange)
  return () => window.removeEventListener('resize', onChange)
}
/** A snapshot key `useSyncExternalStore` can compare by value; split back below. */
const viewportKey = () => [window.innerWidth, window.innerHeight].join(',')

/** The viewport size, re-read on resize (the tracker re-measures the target, not the window). */
function useViewport(): Viewport {
  const key = useSyncExternalStore(subscribeViewport, viewportKey, viewportKey)
  const [w, h] = key.split(',').map(Number)
  return { width: w, height: h }
}

/** The panel's rendered height, followed as its content changes. */
function useMeasuredHeight(): [(el: HTMLElement | null) => void, number] {
  const [el, setEl] = useState<HTMLElement | null>(null)
  const [height, setHeight] = useState(0)
  useEffect(() => {
    if (!el) return
    const read = () => setHeight(el.offsetHeight)
    read()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(read)
    ro.observe(el)
    return () => ro.disconnect()
  }, [el])
  return [setEl, height]
}

/**
 * The one panel element. Anchored next to `rect` when given, else a chip at
 * the bottom centre. Either way it is `position: fixed` in a portal, so it
 * takes no space from the page. Moving between the two forms animates the
 * same element (reduced motion drops the motion, not the element); following
 * a scrolling target snaps, so it never trails the control it describes.
 */
function GuidePanel({ rect, obstacles, reserved, label, reduceMotion, panelRef, onFocus, onBlur, wide = false, children }: {
  rect: GuideRect | null
  obstacles: readonly Box[]
  reserved: readonly Box[]
  label: string
  reduceMotion: boolean
  panelRef: React.MutableRefObject<HTMLElement | null>
  onFocus: (e: React.FocusEvent<HTMLElement>) => void
  onBlur: (e: React.FocusEvent<HTMLElement>) => void
  /** A one-row body (title plus a button) that needs more than the step width. */
  wide?: boolean
  children: React.ReactNode
}) {
  const viewport = useViewport()
  const [measureRef, height] = useMeasuredHeight()
  const ref = useCallback((el: HTMLDivElement | null) => { panelRef.current = el; measureRef(el) }, [panelRef, measureRef])
  const mode = rect ? 'anchored' : 'chip'
  const [settledMode, setSettledMode] = useState(mode)
  const switching = settledMode !== mode
  useEffect(() => {
    if (!switching) return
    const id = setTimeout(() => setSettledMode(mode), 300)
    return () => clearTimeout(id)
  }, [mode, switching])
  // A guide starts at its target: the first placement is not animated from
  // the bottom chip, and while the first target is still being looked up the
  // chip waits briefly instead of flashing at the bottom first. Later moves
  // between the two forms still animate.
  const [anchoredOnce, setAnchoredOnce] = useState(false)
  useEffect(() => { if (rect) setAnchoredOnce(true) }, [rect])
  const animateSwitch = switching && anchoredOnce
  const [chipDue, setChipDue] = useState(false)
  useEffect(() => {
    if (rect || anchoredOnce) return
    const id = setTimeout(() => setChipDue(true), FIRST_TARGET_WAIT_MS)
    return () => clearTimeout(id)
  }, [rect, anchoredOnce])
  const hidden = !rect && !anchoredOnce && !chipDue

  const width = wide
    ? Math.min(WIDE_PANEL_MAX_WIDTH, Math.max(0, viewport.width - 2 * PANEL_MARGIN))
    : panelWidth(viewport.width)
  const place = rect ? popoverPlacement(rect, viewport, height, obstacles, reserved) : null
  const style: React.CSSProperties = place
    ? { top: place.top, left: place.left, width: place.width }
    : { width }
  // The bottom chip sits above the composer, never over what is typed there.
  if (!place) {
    const below = keepClearBoxes(viewport).filter(b => b.bottom >= viewport.height - KEEP_CLEAR_REPLY_PX * 3)
    if (below.length > 0) {
      style.bottom = viewport.height - Math.min(...below.map(b => b.top)) + PANEL_MARGIN
      // On a wide screen the chip sits at the composer's right edge, away
      // from the left-aligned reply it would otherwise cover.
      if (viewport.width >= NARROW) {
        const right = Math.max(...below.map(b => b.right))
        style.left = Math.max(PANEL_MARGIN, Math.min(right - width, viewport.width - width - PANEL_MARGIN))
        style.right = 'auto'
        style.marginLeft = 0
        style.marginRight = 0
      }
    }
  }
  style.maxHeight = Math.max(0, viewport.height - 2 * PANEL_MARGIN)
  return createPortal(
    <motion.div
      ref={ref}
      role="region"
      aria-label={label}
      data-testid="guide-pill"
      data-placement={place ? place.side : 'chip'}
      onFocus={onFocus}
      onBlur={onBlur}
      // No layout animation before the first placement, so the panel never
      // slides in from where it waited while hidden.
      layout={reduceMotion || !anchoredOnce ? false : 'position'}
      transition={{ layout: { duration: animateSwitch ? 0.2 : 0, ease: 'easeOut' } }}
      className={`pointer-events-auto fixed z-[10003] flex flex-col gap-2 overflow-y-auto rounded-xl border border-border bg-card px-3 py-2.5 text-[13px] text-text shadow-lg ${place ? '' : 'left-safe right-safe bottom-safe-offset-4 mx-auto'} ${hidden ? 'invisible' : ''}`}
      style={style}
    >
      {children}
    </motion.div>,
    document.body,
  )
}

const ARROW_ICONS = { up: ArrowUp, down: ArrowDown, left: ArrowLeft, right: ArrowRight } as const
/** The arrow's nudge toward the target. */
const ARROW_NUDGE: Record<ArrowDir, { x?: number[]; y?: number[] }> = { up: { y: [0, ARROW_TRAVEL, 0] }, down: { y: [0, -ARROW_TRAVEL, 0] }, left: { x: [0, ARROW_TRAVEL, 0] }, right: { x: [0, -ARROW_TRAVEL, 0] } }

function GuideArrow({ rect, also = [], obstacles, reduceMotion }: { rect: GuideRect; also?: readonly GuideRect[]; obstacles: readonly Box[]; reduceMotion: boolean }) {
  const place = arrowPlacement(rect, { width: window.innerWidth, height: window.innerHeight }, obstacles)
  const Icon = ARROW_ICONS[place.dir]
  return (
    <>
      {also.map((r, i) => (
        <div
          key={i}
          aria-hidden="true"
          data-testid="guide-target-outline-alt"
          className="pointer-events-none fixed z-[10002] rounded-md border-2 border-dashed border-accent"
          style={{ top: r.top - 4, left: r.left - 4, width: r.width + 8, height: r.height + 8 }}
        />
      ))}
      <div
        aria-hidden="true"
        data-testid="guide-target-outline"
        className="pointer-events-none fixed z-[10002] rounded-md border-2 border-accent"
        style={{ top: rect.top - 4, left: rect.left - 4, width: rect.width + 8, height: rect.height + 8 }}
      />
      <motion.div
        aria-hidden="true"
        data-testid="guide-arrow"
        data-arrow-dir={place.dir}
        className="pointer-events-none fixed z-[10002] text-accent"
        style={{ top: place.top, left: place.left, width: ARROW, height: ARROW }}
        animate={reduceMotion ? undefined : ARROW_NUDGE[place.dir]}
        transition={reduceMotion ? undefined : { duration: 1.2, repeat: Infinity, ease: 'easeInOut' }}
      >
        <Icon size={ARROW} strokeWidth={2.5} />
      </motion.div>
    </>
  )
}

const REFUSAL_KEYS: Record<GuideActionRefusal, string> = {
  unknown_action: 'components.guideLayer.refused_unknown_action',
  invalid_params: 'components.guideLayer.refused_invalid_params',
  unknown_setting: 'components.guideLayer.refused_unknown_setting',
  sensitive_setting: 'components.guideLayer.refused_sensitive_setting',
  unknown_location: 'components.guideLayer.refused_unknown_location',
  location_not_on_this_screen: 'components.guideLayer.refused_location_not_on_this_screen',
  build_mismatch: 'components.guideLayer.refused_build_mismatch',
  sensitive_page: 'components.guideLayer.refused_sensitive_page',
}

/** The `ui.find` search a step runs: its own, or (an `open` step) the control it reveals. */
function findTargetOf(step: GuideStepPlan | null | undefined): { query: FindQuery; key: string } | null {
  if (!step) return null
  if (step.target.kind === 'find') return step.target
  if (step.target.kind === 'find-container' && step.complete.kind === 'reach') {
    const inner = step.complete.targets[0]
    return inner?.kind === 'find' ? inner : null
  }
  return null
}

function findQueryOf(step: GuideStepPlan | null | undefined): FindQuery | null {
  return findTargetOf(step)?.query ?? null
}

/**
 * Whether a step's control is destructive: the gateway said so, or the
 * control a `ui.find` step points at (the match itself, or the container or
 * menu trigger its `open` step points at) is one by its own identity
 * (`isCautionTarget`), whatever language the agent asked in.
 */
export function stepCaution(step: GuideStepPlan, el: HTMLElement | null): boolean {
  if (step.caution) return true
  return (step.target.kind === 'find' || step.target.kind === 'find-container') && !!el && isCautionTarget(el, accessibleName(el))
}

/**
 * A `ui.find` step the page could not point at: no control carries the name
 * and none of the containers the guide may open holds it, so the person is
 * asked to open the one it is in (`not_found`); its only match is part of the
 * agent's own ceiling (also `not_found` to the gateway, said plainly here);
 * or several do (`ambiguous_target`), listed by what surrounds each and
 * numbered, so the person can pick one here or tell the chat its number. The
 * names shown are read from the page here and never sent anywhere.
 */
function FindMissing({ query, findKey, reason }: { query: FindQuery; findKey: string; reason: string }) {
  const { t } = useTranslation()
  const [, setPicked] = useState(0)
  if (reason === 'ambiguous_target') {
    const contexts = findCandidates(query, findKey, isGuideTargetVisible)
    const picked = findPick(findKey)
    return (
      <div className="m-0" role="status" data-testid="guide-find-ambiguous">
        <p className="m-0">{t('components.guideLayer.find_ambiguous', { label: query.label })}</p>
        {contexts.length > 0 && (
          <ol className="m-0 mt-1 flex list-none flex-col gap-1 p-0">
            {contexts.map((c, i) => (
              <li key={i}>
                <Btn
                  className="w-full justify-start text-start"
                  aria-pressed={picked === i + 1}
                  data-testid={`guide-find-pick-${i + 1}`}
                  onClick={() => { setFindPick(findKey, i + 1); setPicked(i + 1) }}
                >
                  {`${i + 1}. ${c || query.label}`}
                </Btn>
              </li>
            ))}
          </ol>
        )}
      </div>
    )
  }
  if (searchByName(query, document, isGuideTargetVisible).result === 'sensitive') {
    return <p className="m-0" role="status" data-testid="guide-find-sensitive">{t('components.guideLayer.find_sensitive', { label: query.label })}</p>
  }
  // A control drawn only while there is something for it (Clear all with no
  // notifications) is not in a menu: say there is nothing for it yet.
  if (query.location && GUIDE_ONLY_WITH_ITEMS.includes(query.location)) {
    return <p className="m-0" role="status" data-testid="guide-find-nothing-yet">{t('components.guideLayer.find_nothing_yet', { label: query.label })}</p>
  }
  return <p className="m-0" role="status" data-testid="guide-find-not-found">{t('components.guideLayer.find_not_found', { label: query.label })}</p>
}

/** Where the finish chip's way back leads: the chat the guide was offered in. */
export type GuideReturn = { kind: 'chat'; to: string }

/**
 * A crewmate thread's slot key (`member-<slug>`, or `member-<slug>.memory-<store>`
 * once the crewmate has a private memory store) -> that slug; anything else
 * -> null. Mirrors the gateway's `member_slot_key` (members.py): the prefix,
 * the slug pattern, and the store suffix that belongs to the SLOT, not the slug.
 */
const MEMBER_SLOT_KEY = /^member-([a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?)(?:\.memory-.+)?$/
export function memberSlugOfSlot(slotKey: string): string | null {
  return MEMBER_SLOT_KEY.exec(slotKey)?.[1] ?? null
}

const toMember = (name: string): GuideReturn => ({ kind: 'chat', to: `/members?member=${encodeURIComponent(name)}` })

/**
 * The way back to *slotKey*'s chat once its guide ended: a crewmate's thread
 * when it is theirs, else the chat page on that slot.
 *
 * A crewmate thread never leads to the chat page, which does not show those
 * slots: when the roster has no row bound to the slot (it failed to load, or
 * the binding moved on), the crewmate is read from the slot key itself.
 * Otherwise `null` while the roster is still unknown, so the way back never
 * changes under the user's pointer.
 */
export function guideReturnFor(
  slotKey: string,
  rows: readonly MemberRosterRow[] | undefined,
  rosterFailed: boolean,
): GuideReturn | null {
  if (!slotKey) return null
  const bound = rows?.find(r => !!r.slot_key && r.slot_key === slotKey)
  if (bound) return toMember(bound.name)
  const slug = memberSlugOfSlot(slotKey)
  if (slug !== null) {
    // The page opens a crewmate by exact name; the slug is lossy, so a name is
    // used only when exactly one row carries this slug.
    const bySlug = rows?.filter(r => r.slug === slug) ?? []
    if (bySlug.length === 1) return toMember(bySlug[0].name)
  }
  if (!rows && !rosterFailed) return null
  // A member slot no roster row names: the page answers a gone crewmate itself.
  if (slug !== null) return toMember(slug)
  return { kind: 'chat', to: `/chat?slot=${encodeURIComponent(slotKey)}` }
}

/**
 * Matches the in-chat offer card's 44px controls (GuideOfferCard). The negative
 * margins let the hit area reach into the panel's padding, so the header row
 * stays one text line tall beside it.
 */
const TOUCH_CLOSE = 'min-h-11 min-w-11 -my-2.5 -mr-2 inline-flex items-center justify-center'

function PillHead({ title, onClose, closeLabel, closeClassName = '', titleRef, action }: {
  title: string
  onClose?: () => void
  closeLabel: string
  closeClassName?: string
  titleRef?: React.Ref<HTMLSpanElement>
  /** A button drawn on the title row, before the close control. */
  action?: React.ReactNode
}) {
  return (
    <div className={`flex gap-2 ${closeClassName ? 'items-center' : 'items-start'}`}>
      <Compass size={16} className={`shrink-0 text-accent ${closeClassName ? '' : 'mt-0.5'}`} aria-hidden="true" />
      <span
        ref={titleRef}
        tabIndex={titleRef ? -1 : undefined}
        data-testid={titleRef ? 'guide-finished-title' : undefined}
        className="min-w-0 flex-1 font-semibold text-text-strong rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {title}
      </span>
      {action}
      {onClose && (
        <IconButton aria-label={closeLabel} title={closeLabel} onClick={onClose} className={`shrink-0 ${closeClassName}`} data-testid={closeClassName ? 'guide-finished-close' : undefined}>
          <X size={14} aria-hidden="true" />
        </IconButton>
      )}
    </div>
  )
}

/**
 * Whether the current step is the guide's last one: the last step of its last
 * action. Its acknowledge button reads Done instead of Next, because nothing
 * follows it. Done only ends the guide; it never stands for the change the
 * step asked for.
 */
export function isFinalGuideStep(view: GuideView): boolean {
  if (!view.resolved.ok || !view.action) return false
  return view.guide.action_index === view.resolved.actions.length - 1
    && view.guide.step_index === view.action.steps.length - 1
}

/**
 * Why the current step's control cannot be drawn right now, in the person's
 * words: one line per unmet runtime predicate. Nothing when none is unmet.
 */
function PredicateBlocker({ unmet }: { unmet: readonly string[] }) {
  const { t } = useTranslation()
  if (unmet.length === 0) return null
  return (
    <div className="m-0 text-muted" role="status" data-testid="guide-blocker">
      <p className="m-0">{t('components.guideLayer.blocked_title')}</p>
      <ul className="m-0 mt-1 ps-4">
        {unmet.map(id => (
          <li key={id}>{isRuntimePredicate(id) ? t(GUIDE_PREDICATE_TEXT_KEYS[id]) : t('components.guideLayer.predicate_unknown')}</li>
        ))}
      </ul>
    </div>
  )
}

/**
 * A gate or select step's verdict (`factVerdict`), re-read on the predicate
 * tick while *enabled*; null for any other step.
 */
function useFactVerdict(step: GuideStepPlan | null | undefined, enabled: boolean): ReturnType<typeof factVerdict> {
  const [verdict, setVerdict] = useState<ReturnType<typeof factVerdict>>(() => (step ? factVerdict(step) : null))
  useEffect(() => {
    if (!step || !enabled) return
    const read = () => setVerdict(factVerdict(step))
    read()
    const t = setInterval(read, GUIDE_PREDICATE_TICK_MS)
    return () => clearInterval(t)
  }, [step, enabled])
  return step ? verdict : null
}

/**
 * The name of the entity open in *selection*, re-read on the predicate tick
 * while a selection is given, so a confirm names what is open now.
 */
function useLiveSelectedName(selection: string | null): string | undefined {
  const [name, setName] = useState<string | undefined>(() => (selection ? selectedName(selection) : undefined))
  useEffect(() => {
    if (!selection) return
    const read = () => setName(selectedName(selection))
    read()
    const t = setInterval(read, GUIDE_PREDICATE_TICK_MS)
    return () => clearInterval(t)
  }, [selection])
  return selection ? name : undefined
}

/**
 * What a gate or select step is waiting on, in the person's words: the gate's
 * own line (naming the setting that turns it on), or an empty picker's.
 * Nothing for any other step, or a select step whose picker has entries.
 */
function FactBlocker({ step, verdict }: { step: GuideStepPlan; verdict: ReturnType<typeof factVerdict> }) {
  const { t } = useTranslation()
  const c = step.complete
  if (c.kind === 'gate' && verdict !== 'done') {
    return <p className="m-0 text-muted" role="status" data-testid="guide-gate-blocker">{t(step.textKey, step.textVars)}</p>
  }
  if (c.kind === 'select' && verdict === 'blocked') {
    const keys = Object.hasOwn(GUIDE_SELECTION_TEXT_KEYS, c.entity) ? GUIDE_SELECTION_TEXT_KEYS[c.entity as keyof typeof GUIDE_SELECTION_TEXT_KEYS] : null
    return <p className="m-0 text-muted" role="status" data-testid="guide-selection-empty">{keys ? t(keys.empty) : t('components.guideLayer.predicate_unknown')}</p>
  }
  return null
}

function ActiveStep({ view, rect, preselected = false, awaitingConfirm = false }: {
  view: GuideView
  rect: GuideRect | null
  /** A select step whose pick was already made, before a step that removes something: confirm it with Next. */
  preselected?: boolean
  /** The destructive control was pressed: its own confirm is being answered. */
  awaitingConfirm?: boolean
}) {
  const { t } = useTranslation()
  const ctx = useGuide()!
  // Re-rendered on every navigation: a page-bound step's Done follows the address.
  useLocation()
  const step = view.step!
  const waiting = ((step.complete.kind === 'committed' && ctx.submitted) || awaitingConfirm) && !rect
  const waitingKey = awaitingConfirm ? 'components.guideLayer.waiting_for_your_answer' : 'components.guideLayer.waiting_for_confirmation'
  // Only while the control is absent: a drawn control is pointed at as it is.
  const unmet = useUnmetPredicates(step.requires, !rect && !waiting && !preselected)
  const c = step.complete
  // The confirm line names the list only while the list is drawn: a picker
  // folded away (a crewmate chat hides the roster) has no list to point at,
  // so the panel floats and says what Next does without sending anyone to it.
  const selectionKeys = preselected && c.kind === 'select' && Object.hasOwn(GUIDE_SELECTION_TEXT_KEYS, c.entity)
    ? GUIDE_SELECTION_TEXT_KEYS[c.entity as keyof typeof GUIDE_SELECTION_TEXT_KEYS]
    : null
  const confirmKey = selectionKeys ? (rect ? selectionKeys.confirm : selectionKeys.confirm_unseen) : null
  // The confirm names the entity actually open, read from its own page, so
  // the step that removes something after it is never about a different one.
  const confirmName = useLiveSelectedName(confirmKey && c.kind === 'select' ? c.selection : null)
  const verdict = useFactVerdict(step, true)
  // A gate step points at nothing, and an empty picker has nothing to point
  // at: their own line stands in for the step text and the spinner.
  const factBlocked = (step.complete.kind === 'gate' && verdict !== 'done') || (step.complete.kind === 'select' && verdict === 'blocked')
  // A `ui.find` target is named as the page names it, not as it was asked for.
  const liveEl = rect && step.target.kind === 'find' ? resolveGuideTarget(step.target) : null
  const liveName = liveEl ? accessibleName(liveEl) : ''
  const textVars = liveName ? { ...step.textVars, label: liveName } : step.textVars
  // A greyed-out control (Forward with no page to go forward to) is pointed
  // at, but "press it" would ask for something it cannot do now.
  const greyedEl = rect && step.complete.kind === 'ack' ? (liveEl ?? resolveGuideTarget(step.target)) : null
  const greyed = !!greyedEl && isDisabled(greyedEl)
  // A list holding one item leaves nothing to choose: name it (on a phone the
  // job list often has just one).
  const onlyItem = rect && step.complete.kind === 'select' ? onlyPickName(step) : null
  // A pick the person named: the panel names the card as it is drawn.
  const namedItem = rect && step.complete.kind === 'select' && !onlyItem && !confirmKey ? namedPickShown(step) : null
  // Nothing to point at by design (a page guide's last step): no search, no spinner.
  const pointless = step.target.kind === 'none'
  const looking = !rect && !pointless && !waiting && !preselected
  // "X is highlighted" is said only once it is: while the control is still
  // being looked for, the status line below is the whole message.
  const claimsHighlight = step.textKey === 'components.guideLayer.step_ui_show_here'
  return (
    <>
      {factBlocked
        ? <FactBlocker step={step} verdict={verdict} />
        : looking && claimsHighlight ? null : (
          <p className="m-0" aria-live="polite" data-testid="guide-step-text">
            {waiting ? t(waitingKey) : confirmKey ? t(confirmKey) : greyed ? t('components.guideLayer.step_target_disabled', { label: textVars?.label ?? '' }) : onlyItem ? t(ONLY_ONE_TEXT_KEYS[c.kind === 'select' ? c.entity : ''] ?? 'components.guideLayer.select_the_only_one', { name: onlyItem }) : t(step.textKey, textVars)}
          </p>
        )}
      {namedItem && (
        <p className="m-0 font-medium text-text-strong" data-testid="guide-pick-name">{t('components.guideLayer.select_pick_named', { name: namedItem })}</p>
      )}
      {confirmName && (
        <p className="m-0 font-medium text-text-strong" data-testid="guide-confirm-name">{t('components.guideLayer.select_open_named', { name: confirmName })}</p>
      )}
      {/* A destructive control: say so plainly before the person presses it. */}
      {stepCaution(step, liveEl) && !factBlocked && (
        <p className="m-0 flex items-start gap-1.5 text-warn" role="note" data-testid="guide-step-caution">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
          {/* The page's own account of what it removes when the index has one:
              a generic "gone for good" would contradict a control that keeps data. */}
          <span>{t(step.cautionKey ?? 'components.guideLayer.step_caution')}</span>
        </p>
      )}
      {/* The offering agent's own words, always under the dashboard's line: the
          intro on the guide's first step, an action's note on that action's
          last. A step that is both shows one block, the note when there is one,
          so a one-step guide never stacks two "From" lines. */}
      {(() => {
        const first = view.guide.action_index === 0 && view.guide.step_index === 0
        const last = !!view.action && view.guide.step_index === view.action.steps.length - 1
        const note = last ? view.guide.actions[view.guide.action_index]?.note : undefined
        if (last && typeof note === 'string' && note.trim()) return <GuideAgentNote text={note} testId="guide-step-note" slotKey={view.guide.slot_key} />
        return first ? <GuideAgentNote text={view.guide.intro} testId="guide-step-intro" slotKey={view.guide.slot_key} /> : null
      })()}
      {!factBlocked && looking && unmet.length > 0 && <PredicateBlocker unmet={unmet} />}
      {!factBlocked && looking && unmet.length === 0 && (
        // Retries exhausted: the wait will not end by itself, so say the guide
        // stopped (with the way back below) instead of spinning forever.
        ctx.stalled
          ? <p className="m-0 text-muted" role="status">{t('components.guideLayer.target_missing')}</p>
          : (
            <p className="m-0 flex items-center gap-1.5 text-muted" role="status">
              <Loader2 size={13} className="animate-spin" aria-hidden="true" /> {t('components.guideLayer.looking_for_control')}
            </p>
          )
      )}
      {ctx.submitted && <p className="m-0 text-[12px] text-muted">{t('components.guideLayer.cancel_does_not_undo')}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Btn onClick={() => ctx.cancel()} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
        {/* The pick was already made: Next goes on with it, whether or not
            the picker is on screen. */}
        {preselected && c.kind === 'select' && !(!rect && !waiting && ctx.stalled) && (
          <Btn
            primary
            onClick={() => {
              // Re-read at the press: the pick must still be the one the
              // person is confirming (the named one, the one this panel names).
              if (!pickMade(step) || (confirmName !== undefined && selectedName(c.selection) !== confirmName)) return
              // What Next confirmed binds the rest of the guide.
              freezePick(step)
              ctx.report('observed')
            }}
            disabled={ctx.busy}
            data-testid="guide-next"
          >
            {isFinalGuideStep(view) ? t('components.meetCrewmatesFlow.done') : t('components.guideLayer.next')}
          </Btn>
        )}
        {/* A stalled ack step shows the way back instead: Next cannot be
            pressed without the control, and a row holds at most two buttons. */}
        {step.complete.kind === 'ack' && !(!rect && !waiting && ctx.stalled) && (
          <Btn primary onClick={() => ctx.report('observed')} disabled={(!rect && !pointless) || offStepRoute(step)} data-testid="guide-next">
            {isFinalGuideStep(view) ? t('components.meetCrewmatesFlow.done') : t('components.guideLayer.next')}
          </Btn>
        )}
        {/* The reports for this step ran out of retries while the control is
            absent: the wait cannot end by itself, so offer the way back,
            which also starts the reports again. */}
        {!rect && !waiting && ctx.stalled && (
          <Btn primary onClick={ctx.returnToStep} disabled={ctx.busy} data-testid="guide-go-back">{t('components.guideLayer.go_back_to_step')}</Btn>
        )}
      </div>
    </>
  )
}

/**
 * A missing step's line: why the control is not drawn when a predicate it
 * needs is unmet, else that it left the page or went missing.
 */
function MissingStep({ view }: { view: GuideView }) {
  const { t } = useTranslation()
  const unmet = useUnmetPredicates(view.step?.requires, !view.offStepPage)
  const verdict = useFactVerdict(view.step, !view.offStepPage)
  const find = findTargetOf(view.step)
  if (!view.offStepPage && find && (view.guide.reason === 'not_found' || view.guide.reason === 'ambiguous_target')) {
    return <FindMissing query={find.query} findKey={find.key} reason={view.guide.reason} />
  }
  if (!view.offStepPage && view.step && (verdict === 'blocked' || (view.step.complete.kind === 'gate' && verdict !== 'done'))) {
    const blocker = <FactBlocker step={view.step} verdict={verdict} />
    if (view.step.complete.kind === 'gate' || view.step.complete.kind === 'select') return blocker
  }
  if (!view.offStepPage && unmet.length > 0) return <PredicateBlocker unmet={unmet} />
  return <p className="m-0" role="status">{view.offStepPage ? t('components.guideLayer.left_step') : t('components.guideLayer.target_missing')}</p>
}

/**
 * Cancel, and the way back to the current step's page, for a step the user is
 * not on (it left its page, or its target went missing). The way back is the
 * action's own enter plan; arriving there resumes the step by itself.
 */
function StepReturnButtons({ back = true }: { back?: boolean }) {
  const { t } = useTranslation()
  const ctx = useGuide()!
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <Btn onClick={() => ctx.cancel()} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
      {back && <Btn primary onClick={ctx.returnToStep} disabled={ctx.busy} data-testid="guide-go-back">{t('components.guideLayer.go_back_to_step')}</Btn>}
    </div>
  )
}

/**
 * Whether a missing step has somewhere to go back to: not a `ui.find` the
 * person's own page could not show (not there, or several), which the guide
 * picks up by itself once the person opens what holds it or picks one.
 */
function hasWayBack(view: GuideView): boolean {
  if (view.offStepPage) return true
  return !(findTargetOf(view.step) && (view.guide.reason === 'not_found' || view.guide.reason === 'ambiguous_target'))
}

export default function GuideLayer() {
  const { t } = useTranslation()
  const ctx = useGuide()
  const reduceMotion = !!useReducedMotion()
  const view = ctx?.view ?? null
  const tracking = !!view && view.ownedHere && !view.needsEnter && !view.leftStep && view.guide.status === 'active' && !!view.step
  // A missing target is watched for, not tracked: once the human is back on
  // the step's page and it shows again, the gateway resumes the same step.
  const recovering = !!view && view.ownedHere && view.guide.status === 'target_missing' && !!view.step
  // The claimed placement is part of the step's identity: a re-plan (the
  // viewport changed) keeps the step index but walks another step list.
  const placementKey = view?.guide.actions[view.guide.action_index]?.placement ?? ''
  const stepId = view ? `${view.guide.guide_id}:${view.guide.action_index}:${view.guide.step_index}:${placementKey}:${ctx?.trackNonce ?? 0}` : ''
  // A choice frozen by one guide (or one action) never binds the next.
  const choiceScope = view ? `${view.guide.guide_id}:${view.guide.action_index}` : ''
  useEffect(() => { clearFrozenPicks() }, [choiceScope])
  // A select step whose pick was already made when it started, with a step
  // that removes something after it in the same action: shown once as a
  // confirm step, so the person says which entity the removal is about.
  // Before anything else it is passed over: there is nothing to confirm.
  const [preselectedStep, setPreselectedStep] = useState('')
  // The step whose destructive control was pressed and whose confirm is open
  // (see the press watch below): never missing meanwhile.
  const [awaitingStep, setAwaitingStep] = useState('')
  const confirmWatch = useRef<(() => void) | null>(null)
  const confirmPreselected = !!view?.action && !!view.step && view.step.complete.kind === 'select'
    && view.action.steps.slice(view.guide.step_index + 1).some(s => s.caution)
  const rect = useGuideStepTracker({
    stepId,
    step: view?.step ?? null,
    enabled: (tracking || recovering) && !ctx?.cancelling,
    recover: recovering,
    suppressMissing: !!ctx?.submitted || (!!stepId && awaitingStep === stepId),
    reduceMotion,
    onObserved: () => ctx?.report('observed'),
    onMissing: (detail) => ctx?.report('target_missing', undefined, detail),
    onFound: (resumeStepIndex) => ctx?.report('target_found', resumeStepIndex),
    earlierSteps: view?.action && view.step ? view.action.steps.slice(0, view.guide.step_index) : undefined,
    onPreselected: confirmPreselected ? () => setPreselectedStep(stepId) : undefined,
  })
  const preselected = confirmPreselected && !!stepId && preselectedStep === stepId
  // A `ui.find` action's `open` step looks inside the page's containers
  // when the control is not already visible (useFindProbe).
  const openFindStep = tracking && view?.action?.id === 'ui.find' && view.step?.target.kind === 'find-container' ? view.step : null
  const openFindTarget = openFindStep?.target.kind === 'find-container' ? openFindStep.target : null
  useFindProbe({ runId: stepId, query: findQueryOf(openFindStep), findKey: openFindTarget?.key ?? null, enabled: !!openFindStep && !ctx?.busy && !ctx?.cancelling, opener: openFindTarget?.opener })
  // A target larger than the screen (a whole table) is outlined, and the
  // panel placed, by the part of it that shows.
  const hasStep = tracking && !!view?.step
  // A modal over the target (the job's details, opened by pressing its row):
  // nothing is drawn across the dialog; the panel waits as a chip.
  const pageModal = useOpenModal(hasStep)
  const coveredTarget = pageModal && !!rect && !!view?.step && coveredByModal(resolveGuideTarget(view.step.target))
  const anchor = useMemo(() => (hasStep && rect && !coveredTarget ? clampToViewport(rect, { width: window.innerWidth, height: window.innerHeight }) : null), [hasStep, rect, coveredTarget])
  // A `ui.show` guide's last step points at the control it was about. Pressing
  // that control is the person doing what the guide led them to, so it ends
  // the guide exactly as Done does, instead of leaving the panel standing over
  // whatever the press just opened. Bubble phase: the control's own handler
  // runs first, so the press still does what it always does. A destructive
  // control (`caution`) is different: its press usually opens a confirm, and
  // the guide must not end on the press alone. It ends once a dialog the press
  // opened has closed again (the person confirmed or backed out), or on Done.
  const finalShowStep = tracking && !!rect && !!view && (view.action?.id === 'ui.show' || view.action?.id === 'ui.find')
    && view.step?.complete.kind === 'ack' && isFinalGuideStep(view)
  const finalStep = finalShowStep ? view?.step ?? null : null
  const reportRef = useRef(ctx?.report)
  reportRef.current = ctx?.report
  // A destructive control's press starts the wait for its confirm. The wait
  // belongs to this step, not to the control: a menu item closes its menu
  // as it is pressed, and the wait must outlive it. While it runs the step
  // is never missing; a confirm ends the guide, anything else returns to
  // the step as it was.
  useEffect(() => () => {
    confirmWatch.current?.()
    confirmWatch.current = null
    setAwaitingStep('')
  }, [stepId])
  useEffect(() => {
    if (!finalStep) return
    const el = resolveStepTarget(finalStep)
    if (!el) return
    if (stepCaution(finalStep, el)) {
      const owner = stepId
      const onCautionPress = () => {
        confirmWatch.current?.()
        // Only an explicit confirm ends the step; cancelled and unknown leave
        // it standing, with Done the person's own.
        confirmWatch.current = watchConfirmDialog(openDialogsNow(), (answer) => {
          confirmWatch.current = null
          setAwaitingStep(s => (s === owner ? '' : s))
          if (answer === 'confirmed') reportRef.current?.('observed')
        }, el)
        setAwaitingStep(owner)
      }
      el.addEventListener('click', onCautionPress)
      return () => el.removeEventListener('click', onCautionPress)
    }
    const onPress = () => { reportRef.current?.('observed') }
    el.addEventListener('click', onPress)
    return () => el.removeEventListener('click', onPress)
  }, [finalStep, stepId])
  // The neighbouring controls the arrow keeps clear of, read from the page as
  // laid out now; the tracker hands back a new rect whenever the target moves.
  // The page can still be moving while the target stays put (a wizard step
  // sliding in after a recovery, a panel expanding), so the boxes the arrow
  // and panel avoid are re-measured on a slow tick too, not only when the
  // target moves; an unchanged measurement keeps the same arrays.
  const [layoutTick, setLayoutTick] = useState(0)
  const anchored = !!anchor
  useEffect(() => {
    if (!anchored) return
    const id = setInterval(() => setLayoutTick(n => n + 1), GUIDE_RELAYOUT_MS)
    return () => clearInterval(id)
  }, [anchored])
  const obstacles = useStableBoxes(useMemo(() => {
    if (!anchor) return NO_BOXES
    const vp = { width: window.innerWidth, height: window.innerHeight }
    return nearbyControlBoxes(anchor, document, Infinity, vp)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchor, layoutTick]))
  const reserved = useStableBoxes(useMemo(() => (anchor ? pageChromeBoxes({ width: window.innerWidth, height: window.innerHeight }) : NO_BOXES),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [anchor, layoutTick]))
  const notePageSeen = ctx?.notePageSeen
  const seenTarget = !!rect && tracking
  // Re-run on arrival too: the target can be found in the render before the
  // address settles, when the page does not yet count as the step's.
  const { pathname } = useLocation()
  useEffect(() => { if (seenTarget) notePageSeen?.() }, [seenTarget, stepId, pathname, notePageSeen])
  const [hiddenPendingError, setHiddenPendingError] = useState<string | null>(null)
  // The guide this tab drove has ended. Its result line is in the chat it was
  // offered in, so the chip speaks only while the user is somewhere else, and
  // offers the way back there.
  const viewedSlot = useViewedSlot()
  const finished = ctx?.finished ?? null
  const showFinished = !!finished && finished.slot_key !== viewedSlot
  const modalOpen = useOpenModal(showFinished)
  const roster = useQuery({ ...membersRosterQuery, enabled: showFinished })
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()

  // Whether keyboard focus is in the panel, kept as focus moves rather than
  // read when the guide ends: by then the Next or Cancel button that was
  // focused may already have left the DOM. Focus leaving for another element,
  // or a press anywhere outside, clears it; an unmount (no new target) does not.
  const panelEl = useRef<HTMLElement | null>(null)
  const focusInside = useRef(false)
  const focusBefore = useRef<HTMLElement | null>(null)
  const onPanelFocus = useCallback((e: React.FocusEvent<HTMLElement>) => {
    if (!focusInside.current) {
      const from = e.relatedTarget
      focusBefore.current = from instanceof HTMLElement && !panelEl.current?.contains(from) ? from : null
    }
    focusInside.current = true
  }, [])
  const onPanelBlur = useCallback((e: React.FocusEvent<HTMLElement>) => {
    const to = e.relatedTarget
    if (to instanceof Node && !panelEl.current?.contains(to)) focusInside.current = false
  }, [])
  // Whether the latest input was a pointer press rather than a key. Focus
  // that a click put in the panel (Done, then focus following to the way
  // back) is not a keyboard user reading it, so it must not hold the chip.
  const lastInputPointer = useRef(false)
  useEffect(() => {
    const down = (e: PointerEvent) => {
      lastInputPointer.current = true
      if (e.target instanceof Node && !panelEl.current?.contains(e.target)) focusInside.current = false
    }
    const key = () => { lastInputPointer.current = false }
    document.addEventListener('pointerdown', down, true)
    document.addEventListener('keydown', key, true)
    return () => {
      document.removeEventListener('pointerdown', down, true)
      document.removeEventListener('keydown', key, true)
    }
  }, [])

  // The guide ended while the user was working in the panel: focus follows
  // to the way back, or to the finish line while no way back is known yet.
  // Never pulled in from elsewhere on the page.
  const finishedTitleRef = useRef<HTMLSpanElement>(null)
  const backRef = useRef<HTMLButtonElement>(null)
  const finishKey = showFinished && finished ? `${finished.guide_id}:${finished.status}` : ''
  useEffect(() => {
    if (!finishKey || !focusInside.current) return
    ;(backRef.current ?? finishedTitleRef.current)?.focus()
  }, [finishKey])

  const dismissFinishedRef = useRef(ctx?.dismissFinished)
  dismissFinishedRef.current = ctx?.dismissFinished
  // The chip leaves GUIDE_FINISHED_DISMISS_MS after the guide ENDED, counted
  // from the context's stamp: this effect re-runs whenever the chip is hidden
  // and shown again (the viewed chat settles after a route change) or the
  // layer remounts, and each run arms only what is left. Only a keyboard user
  // whose focus is in the chip holds it, so it never vanishes from under the
  // keyboard; focus a click left there does not.
  const finishedAt = ctx?.finishedAt ?? null
  useEffect(() => {
    if (!finishKey) return
    let id = 0
    const arm = (ms: number) => {
      id = window.setTimeout(() => {
        if (focusInside.current && !lastInputPointer.current) { arm(GUIDE_FINISHED_DISMISS_MS); return }
        const hadFocus = focusInside.current
        const prior = focusBefore.current
        dismissFinishedRef.current?.()
        focusInside.current = false
        if (!hadFocus) return
        // Hand focus back as closing the chip does, never to the body.
        const target = prior && prior.isConnected && !(prior as HTMLButtonElement).disabled ? prior : document.getElementById('main-content')
        target?.focus()
      }, ms)
    }
    arm(Math.max(0, (finishedAt ?? Date.now()) + GUIDE_FINISHED_DISMISS_MS - Date.now()))
    return () => window.clearTimeout(id)
  }, [finishKey, finishedAt])
  // Pages visited since the guide ended: the first move (often away from the
  // guided page) keeps the chip, the second takes it away.
  const finishedId = finished?.guide_id ?? ''
  const movesSince = useRef({ id: '', path: '', moves: 0 })
  useEffect(() => {
    if (!finishedId) return
    const m = movesSince.current
    if (m.id !== finishedId) {
      movesSince.current = { id: finishedId, path: pathname, moves: 0 }
      return
    }
    if (m.path === pathname) return
    m.path = pathname
    m.moves += 1
    if (m.moves >= 2) dismissFinishedRef.current?.()
  }, [finishedId, pathname])

  if (!ctx) return null
  const finishedText = showFinished && finished
    ? t(finishedKeyFor(finished.status, finished.reason, 'components.guideLayer.finished_cancelled'))
    : ''
  // Closing the chip hands focus back to where it was before the panel took
  // it, else to the page's main region -- never to the document body.
  const closeFinished = () => {
    const restore = focusInside.current
    const prior = focusBefore.current
    ctx.dismissFinished()
    focusInside.current = false
    if (!restore) return
    const target = prior && prior.isConnected && !(prior as HTMLButtonElement).disabled
      ? prior
      : document.getElementById('main-content')
    target?.focus()
  }

  const label = t('components.guideLayer.region_label')
  const closeLabel = t('components.guideLayer.close')
  const error = (
    <>
      {/* No hand-off: the guide pill floats over the page being guided, whose unsaved form draft the hand-off navigation would discard. */}
      <ErrorNotice variant="inline" className="text-[12px]" message={ctx.error} testId="guide-error" />
    </>
  )

  let body: React.ReactNode = null
  if (!view && ctx.pendingError && ctx.pendingError !== hiddenPendingError) {
    body = (
      <>
        <PillHead title={t('components.guideLayer.title_generic')} onClose={() => setHiddenPendingError(ctx.pendingError)} closeLabel={closeLabel} />
        {/* No hand-off: the pill floats over whatever page is open, whose unsaved draft the hand-off navigation would discard. */}
        <ErrorNotice variant="inline" className="text-[12px]" message={ctx.pendingError} testId="guide-pending-error" />
      </>
    )
  } else if (view) {
    const title = view.action ? t(view.action.titleKey, view.action.titleVars) : t('components.guideLayer.title_generic')
    const g = view.guide
    if (!view.resolved.ok) {
      body = (
        <>
          <PillHead title={t('components.guideLayer.title_generic')} closeLabel={closeLabel} />
          <p className="m-0">{t(REFUSAL_KEYS[view.resolved.reason])}</p>
          {error}
          <div className="flex justify-end"><Btn onClick={() => ctx.cancel()} disabled={ctx.busy}>{t('components.guideLayer.dismiss')}</Btn></div>
        </>
      )
    } else if (g.status === 'target_missing' && view.ownedHere) {
      body = (
        <>
          <PillHead title={title} closeLabel={closeLabel} />
          <MissingStep view={view} />
          {error}
          <StepReturnButtons back={hasWayBack(view)} />
        </>
      )
    } else if (g.status === 'offered') {
      // The offer is the chat's to show (GuideOfferCard in that slot's chat),
      // never a banner over whatever page is open.
      body = null
    } else if (!view.ownedHere) {
      body = (
        <>
          <PillHead title={title} closeLabel={closeLabel} />
          <p className="m-0">{t('components.guideLayer.other_tab')}</p>
          {error}
          <div className="flex flex-wrap justify-end gap-2">
            <Btn onClick={() => ctx.cancel()} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
            <Btn primary onClick={ctx.takeOver} disabled={ctx.busy} data-testid="guide-take-over">{t('components.guideLayer.take_over')}</Btn>
          </div>
        </>
      )
    } else if (view.needsEnter) {
      body = (
        <>
          <PillHead title={title} closeLabel={closeLabel} />
          <p className="m-0">{t('components.guideLayer.continue_hint')}</p>
          {error}
          <div className="flex flex-wrap justify-end gap-2">
            <Btn onClick={() => ctx.cancel()} disabled={ctx.busy}>{t('components.guideLayer.cancel_guide')}</Btn>
            <Btn primary onClick={ctx.continueAction} disabled={ctx.busy} data-testid="guide-continue">{t('components.guideLayer.continue')}</Btn>
          </div>
        </>
      )
    } else if (view.leftStep) {
      body = (
        <>
          <PillHead title={title} closeLabel={closeLabel} />
          <p className="m-0" role="status">{t('components.guideLayer.left_step')}</p>
          {error}
          <StepReturnButtons />
        </>
      )
    } else if (view.step) {
      body = (
        <>
          <PillHead title={title} closeLabel={closeLabel} />
          <ActiveStep view={view} rect={rect} preselected={preselected} awaitingConfirm={!!stepId && awaitingStep === stepId} />
          {error}
        </>
      )
    }
  }

  let announced = ''
  // A modal the guide led into (the add-server form) is still open: the
  // finished chip waits until it closes instead of sitting on top of it.
  if (!body && showFinished && finished && !modalOpen) {
    announced = finishedText
    const back = guideReturnFor(finished.slot_key, roster.data, roster.isError)
    const backLabel = t('components.meetCrewmatesFlow.back_to_chat')
    body = (
      <PillHead
        title={finishedText}
        titleRef={finishedTitleRef}
        onClose={closeFinished}
        closeLabel={closeLabel}
        closeClassName={TOUCH_CLOSE}
        action={back && (
          <Btn
            ref={backRef}
            primary
            className="min-h-11 shrink-0 px-4"
            data-testid="guide-back"
            data-guide-return={back.kind}
            onClick={() => guardedLeave(() => { focusInside.current = false; navigate(back.to); ctx.dismissFinished() }, back.to)}
          >
            {backLabel}
          </Btn>
        )}
      />
    )
  }

  return (
    <>
      {/* One status region for the whole life of the layer, so the end of a
          guide is announced into a region that already exists. Silent on the
          slot's own chat, whose result line announces it instead. */}
      {createPortal(<div role="status" aria-live="polite" className="sr-only" data-testid="guide-finished-status">{announced}</div>, document.body)}
      {body && anchor && createPortal(<GuideArrow rect={anchor} also={view?.step ? pickAlternativeRects(view.step, { width: window.innerWidth, height: window.innerHeight }) : []} obstacles={obstacles} reduceMotion={reduceMotion} />, document.body)}
      {body && (
        <GuidePanel rect={anchor} obstacles={obstacles} reserved={reserved} label={label} reduceMotion={reduceMotion} panelRef={panelEl} onFocus={onPanelFocus} onBlur={onPanelBlur} wide={!!announced}>
          {body}
        </GuidePanel>
      )}
    </>
  )
}
