/**
 * Whether the main chat pane is actually on screen right now (#23564).
 *
 * Invariant this serves: a cron note is marked read only when a user gesture
 * opened its chat (or the note arrived live) AND the transcript is visible.
 * "Visible" is the conjunction of three things, each owned by one place:
 *
 *  1. The pane shows the chat's transcript: `useMarkTranscriptShown` (mounted,
 *     loaded, not behind the restore gate). A background session tab, a closed
 *     split pane or a popout's pane does not register here.
 *  2. The user can see this window: not `isWindowAway()` (a background tab or
 *     minimized window via `document.hidden`, another app in front via
 *     `document.hasFocus()`), and the route is a chat route.
 *  3. Nothing is drawn over the pane: `paneCovered()` below, plus the covers
 *     only the bell's own state can know, which its hook passes as `hidden`
 *     (a remote Crew in front, a crew window, the bell's detail card).
 *
 * `paneCovered()` is DOM-level on purpose. The page has dozens of overlays, and
 * wiring each one's open state into this hook would miss the next one added.
 * Two markings cover them:
 *
 *  - every modal dialog already declares `role="dialog"` with
 *    `aria-modal="true"` (Modal, the Radix dialog, the command palette, the
 *    command bar, the side sheets, the shortcuts and update dialogs, ...), read
 *    through `openModalDialog`, the same spelling the guide uses;
 *  - a non-modal surface that is drawn over the pane carries
 *    `data-covers-pane` while it is OPEN: the phone's nav drawer, its
 *    full-width sessions drawer and its side panel. A drawer stays mounted through its
 *    slide-out, and must drop the mark when it starts closing: a tap on a
 *    session row opens that chat and closes the drawer at once, and the
 *    transcript it uncovers is what the user is looking at. A new covering
 *    surface opts in by adding that attribute for exactly its open phase.
 *
 * The marks are the fast path. The backstop closes the class: the chat's own
 * transcript element must be on screen (`elementOnScreen`): a non-empty box,
 * inside the viewport, and a hit test inside it. That catches what no mark
 * names: a surface someone adds later without the attribute, a panel that
 * squeezes the transcript to zero width, a transcript slid off screen.
 *
 * Read at the moment of the event, never on a schedule: a covered pane reads
 * nothing, and nothing is retried when the cover goes away.
 */
import { openModalDialog } from '../guide/openModalDialog'
import { isWindowAway } from './windowAway'

/** Spread onto a non-modal element that, while mounted, covers the chat pane. */
export const COVERS_PANE_ATTR = 'data-covers-pane'
export const coversPaneProps = { [COVERS_PANE_ATTR]: '' } as const

/** Whether a modal dialog, or a surface marked `data-covers-pane`, is over the pane. */
export function paneCovered(root: Document = document): boolean {
  if (openModalDialog(root)) return true
  return root.querySelector(`[${COVERS_PANE_ATTR}]`) !== null
}

/** Where the backstop looks: the visible rect's centre and four inner points.
 *  Any one hitting the transcript is enough, so a small popover or a tooltip
 *  over the exact centre does not count as a cover; anything that hides all
 *  five (a sheet, a panel, a scrim) does. */
const PROBE_POINTS: ReadonlyArray<readonly [number, number]> = [[0.5, 0.5], [0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75]]

/**
 * The backstop for the covers nobody marked: whether *el* is on screen right
 * now. It needs a non-empty box (not squeezed to zero, not `display: none`),
 * some part of it inside the viewport, and a hit test at one of the probe
 * points of that visible part landing inside *el*, which fails when any
 * opaque surface is drawn over it, marked or not.
 *
 * Limit: `elementFromPoint` skips a surface with `pointer-events: none`, so a
 * cover that lets clicks through is not seen here. Such a surface opts in
 * with `data-covers-pane`, like the drawers.
 */
export function elementOnScreen(el: Element, root: Document = document): boolean {
  const r = el.getBoundingClientRect()
  if (!(r.width > 0 && r.height > 0)) return false
  const vw = root.defaultView?.innerWidth ?? 0
  const vh = root.defaultView?.innerHeight ?? 0
  const left = Math.max(r.left, 0)
  const top = Math.max(r.top, 0)
  const right = Math.min(r.right, vw)
  const bottom = Math.min(r.bottom, vh)
  if (!(right > left && bottom > top)) return false
  // No hit testing (some test environments): the box check stands alone.
  if (typeof root.elementFromPoint !== 'function') return true
  return PROBE_POINTS.some(([fx, fy]) => {
    const hit = root.elementFromPoint(left + (right - left) * fx, top + (bottom - top) * fy)
    return hit !== null && el.contains(hit)
  })
}

/**
 * Whether the user can see the chat pane now: not away, nothing marked over
 * it, and, given the transcript elements of the panes showing the chat, at
 * least one of them actually on screen (`elementOnScreen`). Omitting them
 * skips only the backstop.
 */
export function paneVisibleNow(transcripts?: readonly Element[]): boolean {
  // With no document (server-side, a worker) nobody is looking.
  if (typeof document === 'undefined') return false
  if (isWindowAway() || paneCovered(document)) return false
  return transcripts === undefined || transcripts.some(el => elementOnScreen(el, document))
}
