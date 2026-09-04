/**
 * Preview flags — local, per-device opt-ins for surfaces that ship in the
 * bundle but are NOT ready to be released.
 *
 * The problem this solves: a surface can be code-complete enough to merge and
 * still be too rough to put in front of every user. Deleting it to hold the
 * release loses the work and the review history; shipping it visible releases
 * an unpolished page. A preview flag keeps the code on `main`, keeps the route
 * routable, and simply does not advertise the surface anywhere in the UI until
 * the operator turns it on from Developer > Feature Previews.
 *
 * Deliberately localStorage, not backend config: this is a per-device "show me
 * the unfinished thing" switch with no server behavior attached (the surface's
 * own API is unaffected either way), which is exactly the shape of the existing
 * Developer Mode gate (`mc-dev-mode`). Putting it in `config.json` would imply
 * a fleet-wide setting and a backend contract that does not exist.
 *
 * Retiring a flag is the goal, not an afterthought: when the surface is
 * polished, delete its `previewFlag` from the registry entry and its card from
 * Developer > Feature Previews. The stale localStorage key then reads as an
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
 * Crew: the Crew Members page (`/members`) and the "New Crew Mode chat" entry in
 * the sidebar's create menu.
 *
 * ONE flag over both, not one each: they are two doors into the same unfinished
 * feature, and a user who reaches crew through the door that was left open hits
 * the same rough edges either way — so a per-door flag would only let the
 * feature half-ship. The two surfaces stay separate code; the flag is what says
 * "crew is not released yet".
 *
 * Gating the INGRESS only. A session already created in crew mode keeps working,
 * keeps its `Crew` row badge, and its route stays registered, so turning the
 * flag off does not orphan existing work — it stops advertising the feature to
 * someone who has not opted in.
 */
export const PREVIEW_CREW = `${PREVIEW_FLAG_PREFIX}crew`

/**
 * Creating a chat that RUNS ON a connected remote crew — the "New chat on crew"
 * entry in the sidebar's create menu.
 *
 * Its own flag, deliberately NOT {@link PREVIEW_CREW}. The word "crew" carries
 * two unrelated meanings here: `PREVIEW_CREW` holds Crew Mode (parallel
 * sub-sessions) and the Crew Members page, while this holds sessions dispatched
 * to another MACHINE over the instances tunnel. Sharing one key would release or
 * hold both at once, which is the same half-ship failure `PREVIEW_CREW`'s own
 * one-flag-two-doors reasoning exists to prevent — in the opposite direction.
 *
 * Held because the LANDING is unfinished, not the dispatch: the session really is
 * created on the peer, but there is no native remote chat view yet, so it opens
 * by switching to that crew's pane, and the local session list does not show
 * live remote sessions — so the session is hard to return to afterwards.
 *
 * Its toggle lives in Developer > Feature Previews, alongside every other
 * unreleased surface, and NOT on Settings > Remote crews where it started: a
 * held feature is found by looking at the one page that lists held features, so
 * scattering an opt-in onto the page it happens to act on hides it from the only
 * reader who wants it. It keeps its own card there rather than sharing
 * {@link PREVIEW_CREW}'s, for the two-meanings reason above.
 *
 * Gating the INGRESS only. A session already created on a peer keeps running
 * there and stays reachable through that crew's own dashboard; turning the flag
 * off only stops offering the menu entry.
 */
export const PREVIEW_REMOTE_CREW_CHAT = `${PREVIEW_FLAG_PREFIX}remote-crew-chat`

/**
 * A connected remote instance's live sessions, merged into the Sessions list.
 *
 * Gates a surface INSIDE `ChatSidebar`, which every dashboard user renders — so
 * unlike a route-level gate, this flag is also what keeps the per-instance slot
 * queries off the wire for anyone who has not opted in. Read it in the sidebar
 * and skip the fetch, rather than fetching and hiding the rows.
 */
export const PREVIEW_INSTANCE_SESSIONS = `${PREVIEW_FLAG_PREFIX}instance-sessions`

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
