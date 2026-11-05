import { flushSync } from 'react-dom'
import { appPathname } from './basePath'
import { openModalDialog } from '../guide/openModalDialog'

/**
 * Continuity across the phone/desktop form switch.
 *
 * Folding or unfolding a foldable phone, or rotating a tablet, resizes the
 * viewport across the `md` breakpoint with the dashboard open, and the shell
 * swaps its grid between the phone form (one column) and the desktop form
 * (nav, sessions sidebar, chat). `useIsMobile` hands that swap to
 * `runLayoutFormChange`, which:
 *
 *  - keeps keyboard focus where it was. The reading position is the
 *    virtualizer's to keep, and this adds nothing on top of it: unfolding keeps
 *    the row on top, and a fold (desktop to phone) can still move it, a
 *    virtualizer defect tracked on its own;
 *  - where the same-document View Transitions API exists, runs the swap inside
 *    `document.startViewTransition`: the transcript morphs in place into its new
 *    box and the sessions sidebar slides in from the leading edge (unfold) or
 *    out to it (fold). The keyframes are in `styles/layout-morph.css` and use
 *    transform and opacity only.
 *
 * The animation runs only for a form change a live resize caused, while the page
 * is visible, with no modal dialog open, outside `prefers-reduced-motion:
 * reduce`, and outside the embedded view. Anything else (the API missing, a change noticed on `pageshow` or
 * `visibilitychange`, first load) swaps instantly as before. Route changes and
 * sidebar toggles never reach this function.
 */

/** Signals that mean the viewport is resizing in front of the reader. */
const LIVE_CAUSES: ReadonlySet<string> = new Set(['change', 'resize', 'orientationchange'])

/** The CSS hooks `styles/layout-morph.css` keys on. Exported for the tests. */
export const MORPH_ATTR = 'layoutMorph'
export const MORPH_EDGE_ATTR = 'layoutMorphEdge'
export const TRANSCRIPT_ATTR = 'data-vt-transcript'
export const COMPOSER_ATTR = 'data-vt-composer'

export type LayoutFormChange = {
  /** The form the viewport is switching TO: phone (`true`) or desktop. */
  toNarrow: boolean
  /** The event type that noticed the change. */
  cause: string
}

/** The composer dock that belongs to `scroller`'s pane: the first one under
 *  the nearest ancestor that holds one. It travels with the transcript, or the
 *  transcript would slide across a composer already drawn at its new place. */
function composerDockFor(scroller: HTMLElement | null): HTMLElement | null {
  for (let n = scroller?.parentElement ?? null; n && n !== document.body; n = n.parentElement) {
    const dock = n.querySelector<HTMLElement>('[data-testid="composer-dock-root"]')
    if (dock) return dock
  }
  return null
}

/** A form switch is being animated right now. Read during render by surfaces
 *  that animate their own mount or unmount (OverlayDrawer): the transition
 *  moves them, so their own motion would play inside the new snapshot as a
 *  second, competing one. */
export function layoutMorphActive(): boolean {
  return typeof document !== 'undefined' && !!document.documentElement.dataset[MORPH_ATTR]
}

type ViewTransitionLike = { finished: Promise<unknown> }
type StartViewTransition = (update: () => void) => ViewTransitionLike

/** The side the sessions sidebar enters from: the inline-start edge. */
export function leadingEdge(root: HTMLElement = document.documentElement): 'left' | 'right' {
  // The `dir` attribute first: it is what the UI sets, and engines that do not
  // resolve `direction` from it (jsdom) would otherwise report ltr. Computed
  // `direction` covers a stylesheet that sets it without the attribute.
  const attr = root.getAttribute('dir')
  const dir = attr === 'rtl' || attr === 'ltr' ? attr : (typeof getComputedStyle === 'function' ? getComputedStyle(root).direction : '')
  return dir === 'rtl' ? 'right' : 'left'
}

/** A modal dialog covers the page. Named view-transition snapshots paint above
 *  the root snapshot, and the dialog lives in the root one, so animating under
 *  it would draw the transcript and composer over the dialog for the length of
 *  the transition. `openModalDialog` is the shared ARIA test; a Radix dialog
 *  (`ui/dialog`) marks itself `role="dialog"` with `data-state="open"` instead,
 *  and a Radix popover, which renders the same pair inside its popper wrapper,
 *  is not modal. */
function modalDialogOpen(): boolean {
  if (openModalDialog()) return true
  for (const el of document.querySelectorAll('[role="dialog"][data-state="open"]')) {
    if (!el.closest('[data-radix-popper-content-wrapper]')) return true
  }
  return false
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches === true
}

/** The open chat's transcript scroller. With split panes there is one per pane;
 *  the one sharing the deepest ancestor with the focused element is the pane
 *  the reader is in, and the first in document order otherwise. Only one
 *  element may carry a view-transition name, so exactly one is picked. */
function transcriptScroller(): HTMLElement | null {
  const all = [...document.querySelectorAll<HTMLElement>('.chat-container')]
  if (all.length < 2) return all[0] ?? null
  const active = document.activeElement
  const depth = (el: HTMLElement) => {
    let n: Element | null = el
    let d = 0
    while (n && !(active && n.contains(active))) { n = n.parentElement; d++ }
    return n ? d : Infinity
  }
  return all.reduce((best, el) => (depth(el) < depth(best) ? el : best), all[0])
}

function restoreFocus(prev: Element | null): void {
  if (!(prev instanceof HTMLElement) || !prev.isConnected || document.activeElement === prev) return
  if (document.activeElement && document.activeElement !== document.body) return
  prev.focus({ preventScroll: true })
}

/** The transition now in flight. Starting a new one skips the old one, whose
 *  `finished` then settles while the new one is still running: only the newest
 *  transition may remove the morph markers, or the new one loses every rule in
 *  `layout-morph.css` and plays the browser's default crossfade. */
let morphGen = 0
let clearMarks: (() => void) | null = null

export function runLayoutFormChange(apply: () => void, change: LayoutFormChange): void {
  if (!LIVE_CAUSES.has(change.cause) || document.visibilityState === 'hidden') { apply(); return }

  const scroller = transcriptScroller()
  const focused = document.activeElement
  const update = () => {
    flushSync(apply)
    restoreFocus(focused)
  }

  const start = (document as Document & { startViewTransition?: StartViewTransition }).startViewTransition
  if (typeof start !== 'function' || prefersReducedMotion() || modalDialogOpen() || appPathname().startsWith('/embed/')) {
    update()
    return
  }

  // A transition still running hands its markers over: clear them first, so a
  // node it named never carries a view-transition name beside the new one's.
  clearMarks?.()
  const gen = ++morphGen
  const root = document.documentElement
  root.dataset[MORPH_ATTR] = change.toNarrow ? 'fold' : 'unfold'
  root.dataset[MORPH_EDGE_ATTR] = leadingEdge(root)
  const dock = composerDockFor(scroller)
  scroller?.setAttribute(TRANSCRIPT_ATTR, '')
  dock?.setAttribute(COMPOSER_ATTR, '')
  const clear = () => {
    delete root.dataset[MORPH_ATTR]
    delete root.dataset[MORPH_EDGE_ATTR]
    scroller?.removeAttribute(TRANSCRIPT_ATTR)
    dock?.removeAttribute(COMPOSER_ATTR)
    clearMarks = null
  }
  clearMarks = clear
  const cleanup = () => { if (gen === morphGen) clear() }
  let transition: ViewTransitionLike
  try {
    transition = start.call(document, update)
  } catch {
    // The API refused to start: switch at once, as without it.
    clear()
    update()
    return
  }
  transition.finished.then(cleanup, cleanup)
}
