/**
 * Preview flags — local, per-device opt-ins for surfaces that ship in the
 * bundle but are NOT ready to be released.
 *
 * The problem this solves: a surface can be code-complete enough to merge and
 * still be too rough to put in front of every user. Deleting it to hold the
 * release loses the work and the review history; shipping it visible releases
 * an unpolished page. A preview flag keeps the code on `main`, keeps the route
 * routable, and simply does not advertise the surface anywhere in the UI until
 * the operator turns it on from Developer > Config.
 *
 * Deliberately localStorage, not backend config: this is a per-device "show me
 * the unfinished thing" switch with no server behavior attached (the surface's
 * own API is unaffected either way), which is exactly the shape of the existing
 * Developer Mode gate (`mc-dev-mode`). Putting it in `config.json` would imply
 * a fleet-wide setting and a backend contract that does not exist.
 *
 * Retiring a flag is the goal, not an afterthought: when the surface is
 * polished, delete its `previewFlag` from the registry entry and its row from
 * the Developer > Config card. The stale localStorage key then reads as an
 * ordinary unused key and no longer gates anything.
 */
import { safeGetItem, safeSetItem } from './safeStorage'

/**
 * Fired on the window whenever a preview flag changes, so the nav rail updates
 * in the same tick as the toggle instead of waiting for a reload.
 *
 * Mirrors `mc-dev-mode-changed`. One event for all flags (the `detail` names
 * which one) rather than one event per flag, so adding a flag stays a data
 * change.
 */
export const PREVIEW_FLAG_EVENT = 'mc-preview-flag-changed'

/**
 * Shared prefix of every preview-flag storage key.
 *
 * Cross-tab `storage` listeners match on this rather than on a list of known
 * flags, so adding a flag stays a one-line data change.
 */
export const PREVIEW_FLAG_PREFIX = 'mc-preview-'

/** Payload of {@link PREVIEW_FLAG_EVENT}. */
export interface PreviewFlagChange {
  key: string
  on: boolean
}

/** Inbound webhooks (`/webhooks`): functional, not yet polished enough to ship. */
export const PREVIEW_WEBHOOKS = `${PREVIEW_FLAG_PREFIX}webhooks`

/**
 * Read a preview flag. Absent, unparseable, or storage-denied all mean OFF —
 * the whole point of the gate is that a surface stays hidden unless someone
 * deliberately turned it on, so it fails closed.
 */
export function readPreviewFlag(flag: string): boolean {
  return safeGetItem(flag) === '1'
}

/**
 * Write a preview flag and announce it.
 *
 * Returns whether the write actually landed. The announcement is gated on that
 * result, and the gating is load-bearing rather than tidiness: every READER of a
 * flag (`readPreviewFlag`, and so `surfacePreviewEnabled` and the nav rail) goes
 * to storage, while `usePreviewFlag` tracks the event. So dispatching after a
 * dropped write would leave the toggle rendering ON while the rail and Search
 * Everywhere stayed empty — the card contradicting the thing it controls — and
 * the "preference" would vanish on the next reload. Storage writes really can be
 * refused: a locked-down embedding context denies access outright, and an
 * exhausted quota survives `safeSetItem`'s reclaim attempts.
 *
 * On failure the toggle simply stays where it was, which is the truthful
 * outcome: nothing was saved.
 */
export function setPreviewFlag(flag: string, on: boolean): boolean {
  if (!safeSetItem(flag, on ? '1' : '0')) return false
  const detail: PreviewFlagChange = { key: flag, on }
  window.dispatchEvent(new CustomEvent<PreviewFlagChange>(PREVIEW_FLAG_EVENT, { detail }))
  return true
}
