/**
 * The live target registry: what THIS tab shows of a registered UI location
 * right now, and whether each reveal scope is open.
 *
 * Wiring (deliberately not a hook at each control): the render-site marker
 * every curated location already carries, `{...uiLocation(id)}`, IS the target
 * registration. Its callback ref puts the element in the trusted target
 * registry (`../uiLocations/targetRegistry.ts`) while React has it mounted, so
 * a control is registered exactly while it is in the document, there is no
 * second table that could drift from the markers the generator proved, and
 * DOM that only copies the attribute string is never a target. Scope
 * owners are few and must report a state the DOM cannot show (a closed
 * container renders nothing), so they register explicitly through
 * `<GuideRevealScope>`.
 *
 * Nothing here reads text. A status is one of {@link LiveTargetStatus}, a scope
 * is open or not; that is all an observation can carry off the page.
 */
import { inUntrustedContent } from './guideMarkers'
import { GUIDE_OBSERVABLE_IDS } from '../uiLocations/guidePlans.gen'
import { registeredCopies } from '../uiLocations/targetRegistry'
import { isAutoSiteId, UI_AUTO_BUILD_DIGEST } from '../uiLocations/autoBuild'

export type LiveTargetStatus =
  /** Exactly one copy is displayed, enabled and inside the viewport. */
  | 'pointable'
  /** Exactly one copy is displayed and enabled, but scrolled out of the viewport. */
  | 'offscreen'
  /** Mounted, but every copy is hidden (display/visibility, `hidden`, `inert`, an empty box). */
  | 'hidden'
  /** No element carries the id: the control is not rendered at all. */
  | 'unmounted'
  /** Exactly one copy is displayed, but it is disabled (`disabled`, `aria-disabled`). */
  | 'disabled'
  /** Two or more copies are displayed at once: the guide cannot tell which is meant. */
  | 'ambiguous'
  /** Not a location this build can observe. */
  | 'unknown'

export const LIVE_TARGET_STATUSES: readonly LiveTargetStatus[] = [
  'pointable', 'offscreen', 'hidden', 'unmounted', 'disabled', 'ambiguous', 'unknown',
]

const OBSERVABLE: ReadonlySet<string> = new Set(GUIDE_OBSERVABLE_IDS)

/**
 * Whether *id* is a location this build can observe: a curated location, or
 * an auto render site when this bundle was stamped (a non-empty auto digest).
 * The gateway names only auto sites of the auto tier it read, digest-checked;
 * here the stamped marker itself is the registration.
 */
export function isObservableLocation(id: string): boolean {
  return OBSERVABLE.has(id) || (UI_AUTO_BUILD_DIGEST !== '' && isAutoSiteId(id))
}

/** Rendered and painted: connected, non-empty box, not hidden or inert. */
export function isDisplayed(el: HTMLElement | null): el is HTMLElement {
  if (!el || !el.isConnected) return false
  // Agent- or file-authored content is never a control the guide points at.
  if (inUntrustedContent(el)) return false
  if (el.closest('[hidden], [inert]')) return false
  const style = window.getComputedStyle(el)
  if (style.visibility === 'hidden' || style.display === 'none') return false
  const r = el.getBoundingClientRect()
  return r.width > 0 && r.height > 0
}

/** Disabled for the person: the element itself, an `aria-disabled` ancestor, or a disabled fieldset. */
export function isDisabled(el: HTMLElement): boolean {
  if ((el as HTMLButtonElement).disabled === true) return true
  return !!el.closest('[aria-disabled="true"], fieldset[disabled]')
}

/** Whether any part of *el*'s box is inside the viewport. */
export function intersectsViewport(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect()
  const w = window.innerWidth || document.documentElement.clientWidth
  const h = window.innerHeight || document.documentElement.clientHeight
  return r.bottom > 0 && r.right > 0 && r.top < h && r.left < w
}

/**
 * The exactly-one rule, shared by the guide's pointer and the live registry:
 * of *all* copies, the ONE that *isShown* accepts, or none when there are none
 * or several. A hidden copy (the other layout's, kept mounted) is not a rival.
 */
export function soleShown(all: Iterable<HTMLElement>, isShown: (el: HTMLElement) => boolean): { element: HTMLElement | null; shown: number } {
  let element: HTMLElement | null = null
  let shown = 0
  for (const el of all) {
    if (!isShown(el)) continue
    shown += 1
    element = shown === 1 ? el : null
  }
  return { element, shown }
}

/**
 * A repeated render site (one auto site drawn once per list row): the first
 * copy *isShown* accepts, preferring one inside the viewport. Every copy is
 * the same control on a different item, so any one shows where it is; the
 * exactly-one rule (`soleShown`) stays for curated ids and destructive steps.
 */
export function firstShown(all: Iterable<HTMLElement>, isShown: (el: HTMLElement) => boolean): HTMLElement | null {
  let first: HTMLElement | null = null
  for (const el of all) {
    if (!isShown(el)) continue
    if (intersectsViewport(el)) return el
    first ??= el
  }
  return first
}

/**
 * Every element React registered as location *id* (`uiLocation(id)`), or,
 * for an auto site id, the reviewed primitive registered under its stamped
 * site too. Only the registry counts: an element that merely carries the
 * attribute string (an SVG artifact, a markdown reply) is not a copy. One
 * exactly-one rule for both, so an auto site rendered twice is ambiguous like
 * a curated one.
 */
export function uiLocationCopies(id: string): HTMLElement[] {
  const curated = registeredCopies('location', id)
  return isAutoSiteId(id) ? [...curated, ...registeredCopies('auto', id)] : curated
}

const copiesOf = uiLocationCopies

export interface LiveTarget {
  status: LiveTargetStatus
  /** The one displayed copy, for `pointable`, `offscreen` and `disabled`; else null. */
  element: HTMLElement | null
}

/** What this tab shows of location *id* right now. */
export function liveTarget(id: string): LiveTarget {
  if (!isObservableLocation(id)) return { status: 'unknown', element: null }
  const all = copiesOf(id)
  if (all.length === 0) return { status: 'unmounted', element: null }
  const { element, shown } = soleShown(all, isDisplayed)
  if (shown === 0) return { status: 'hidden', element: null }
  if (!element) return { status: 'ambiguous', element: null }
  if (isDisabled(element)) return { status: 'disabled', element }
  return { status: intersectsViewport(element) ? 'pointable' : 'offscreen', element }
}

// ── reveal scopes ──

const scopes = new Map<string, Map<symbol, boolean>>()

/** A scope owner says whether its container is open. One entry per mounted owner. */
export function reportScope(id: string, owner: symbol, open: boolean): void {
  let held = scopes.get(id)
  if (!held) scopes.set(id, (held = new Map()))
  held.set(owner, open)
}

/** A scope owner unmounted: it says nothing any more. */
export function dropScope(id: string, owner: symbol): void {
  const held = scopes.get(id)
  if (!held) return
  held.delete(owner)
  if (held.size === 0) scopes.delete(id)
}

/**
 * Whether scope *id* is open: true when a mounted owner reports it open, false
 * when every mounted owner reports it closed, null when no owner is mounted
 * (nothing is known; a step then falls back to its `reach` targets).
 */
export function scopeOpen(id: string): boolean | null {
  const held = scopes.get(id)
  if (!held || held.size === 0) return null
  for (const open of held.values()) if (open) return true
  return false
}
