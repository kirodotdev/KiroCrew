/**
 * Preview flags — opt-ins for surfaces that ship in the
 * bundle but are NOT ready to be released.
 *
 * The problem this solves: a surface can be code-complete enough to merge and
 * still be too rough to put in front of every user. Deleting it to hold the
 * release loses the work and the review history; shipping it visible releases
 * an unpolished page. A preview flag keeps the code on `main`, keeps the route
 * routable, and simply does not advertise the surface anywhere in the UI until
 * the operator turns it on from Settings > Developer > Feature Previews.
 *
 * Stored in the gateway config (`dashboard.feature_previews.<name>`), with
 * localStorage as a synchronous cache. A switch kept only in the browser is
 * lost to every fresh origin: another browser or device, the desktop app beside
 * a browser tab, a changed dashboard port, or cleared site data. Every reader
 * below still reads the cache, so the nav rail and the registry stay
 * synchronous; `usePreviewFlagServerSync` fills the cache from the server copy
 * and sends each toggle to the server through the writer it registers with
 * {@link setPreviewFlagPersister}.
 * A flag has no server behavior of its own unless its own note says so
 * ({@link PREVIEW_MATE_SESSIONS} is the one that does).
 *
 * Retiring a flag is the goal, not an afterthought: when the surface is
 * polished, delete its `previewFlag` from the registry entry and its card from
 * Settings > Developer > Feature Previews. The stale localStorage key then reads as an
 * ordinary unused key and no longer gates anything. The card's "See what it
 * looks like" intro goes with it: its builder in `FeaturePreviewsSection.tsx`,
 * its captures under `public/app-assets/feature-previews/`, its
 * `pages.developer.featurePreviewsTab.intro.*` keys in every catalog, and its
 * `shoot` step in `scripts/capture-feature-previews.mjs` — the media is the
 * heaviest thing a flag ships, so it must not outlive the flag.
 */
import { LOCKED_FEATURE_PREVIEWS, controlLocked } from './editionLocks'
import { safeGetItem, safeSetItem } from './safeStorage'

/**
 * Fired on the window whenever a preview flag changes, so the nav rail updates
 * in the same tick as the toggle instead of waiting for a reload.
 *
 * One event for all flags (the `detail` names which one) rather than one event per flag, so adding a flag stays a data
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

/** Developer-only Session Environments configuration. Reveals settings, not runtime availability. */
export const PREVIEW_SESSION_ENVIRONMENTS = `${PREVIEW_FLAG_PREFIX}session-environments`

/** Inbound webhooks (`/webhooks`): functional, not yet polished enough to ship. */
export const PREVIEW_WEBHOOKS = `${PREVIEW_FLAG_PREFIX}webhooks`

/**
 * Artifact Deploy (`/deploy`): publishing an artifact to a public HTTPS URL in
 * the operator's own AWS account.
 *
 * Gating the INGRESS only, like every flag here. `/deploy` stays routable, the
 * deploy API is untouched, and an existing deployment keeps working — what the
 * flag controls is whether the product OFFERS the surface to someone who has not
 * asked for it. The doors are the Artifacts page's Artifact Deploy button and its
 * dropdown twin, the webapp card's Deploy hero, and the "Publish to public web
 * (your AWS)" row in the publish panel.
 *
 * Default OFF because every door leads to spending money in a real AWS account
 * and to content served on the open internet. That is not a reasonable default
 * for a surface still settling.
 */
export const PREVIEW_ARTIFACT_DEPLOY = `${PREVIEW_FLAG_PREFIX}artifact-deploy`

/**
 * Send a pending tool approval to the notification bridge: when an approval card
 * opens, the gateway hands the bridge one note naming the request
 * (`notifications/bridge_approval_note.py`), so a connected chat app hears about
 * it. The dashboard bell is not rung for it. Server-only, default OFF.
 */
export const PREVIEW_BRIDGE_APPROVALS = `${PREVIEW_FLAG_PREFIX}bridge-approvals`

/**
 * Creating a chat that RUNS ON a connected remote crew — the "New chat on crew"
 * entry in the sidebar's create menu: sessions dispatched to another MACHINE
 * over the instances tunnel.
 *
 * ONE flag for the whole feature: it also merges each connected crew's live
 * sessions into the Sessions list, grouped per crew. It used to share that half
 * with {@link PREVIEW_INSTANCE_SESSIONS}, and a user with one switch on and the
 * other off could open a chat on a crew and then find no crew group to return
 * to it from. Read it through `useRemoteCrewChat` and write it through
 * {@link setRemoteCrewChat}, never as a bare flag, so the retired key keeps
 * counting.
 *
 * Its toggle lives in Settings > Developer > Feature Previews, alongside every other
 * unreleased surface, and NOT on Settings > Remote Crew where it started: a
 * held feature is found by looking at the one page that lists held features, so
 * scattering an opt-in onto the page it happens to act on hides it from the only
 * reader who wants it.
 *
 * Gating the INGRESS only. A session already created on a peer keeps running
 * there and stays reachable through that crew's own dashboard; turning the flag
 * off only stops offering the menu entry.
 */
export const PREVIEW_REMOTE_CREW_CHAT = `${PREVIEW_FLAG_PREFIX}remote-crew-chat`

/**
 * RETIRED: the old "Remote crew sessions" switch, folded into
 * {@link PREVIEW_REMOTE_CREW_CHAT}. `useRemoteCrewChat` reads either key as
 * ON, {@link setRemoteCrewChat} clears it when the feature is turned off, and
 * `usePreviewFlagServerSync` clears it when it mounts. Never
 * gate anything on it directly.
 */
export const PREVIEW_INSTANCE_SESSIONS = `${PREVIEW_FLAG_PREFIX}instance-sessions`

/**
 * The composable-layout dev harness (`/layout-harness`).
 *
 * Held because it is a MECHANISM being built beside the Members page, not a
 * shippable surface: it mounts the layout renderer + scope over a hand-authored
 * seed so the "panes connect by placement" mechanism can be verified in
 * isolation, and it changes nothing a user sees. Gating the INGRESS only —
 * turning it off hides the route; it orphans nothing (the harness holds no saved
 * state). Its own flag so it releases (or is retired) independently.
 *
 * The key string is `layout-harness`, matching the flag the layout feature
 * itself reads, so the toggle here and the feature's own gate agree on one key.
 */
export const PREVIEW_LAYOUT_HARNESS = `${PREVIEW_FLAG_PREFIX}layout-harness`

/**
 * A sidebar folder drawn around the session that conducts it: that card pinned
 * first inside the folder, with an accent bar down its left edge and a
 * `conductor` tag beside its agent name.
 *
 * Held because it changes the SHAPE of a list tens of thousands of people read
 * every day, by default, for every folder whose sessions were opened by one of
 * them. The pin is not a new door that a reader can decline to walk through, so
 * the usual ingress-only argument for shipping a preview unflagged does not
 * apply here: there is nothing to decline. First Principles review asked for
 * either a flag or an accepted RFC, and a preview flag is the cheaper of the
 * two to retire.
 *
 * Default OFF, and OFF is byte-for-byte the list on `main`: the folder's rows
 * are handed on as the same array in the same order and the body is the same
 * element, so nothing about an ordinary render is conditional on this key.
 *
 * It does NOT gate the two ways to REACH that session -- the folder row's chat
 * button and the same action in the folder's menus. Those are ingresses a reader
 * can decline, they move nothing on the resting list, and gating them here would
 * leave a conducted folder with no way to the conversation it is drawn around.
 * This key holds the layout and nothing else.
 */
export const PREVIEW_FOLDER_CONDUCTOR = `${PREVIEW_FLAG_PREFIX}folder-conductor`

/**
 * Crew Mode: the composer switch that runs a chat on a conductor, and every
 * dashboard mark that goes with it (the welcome, the Crew panel tab, the crew
 * card in the Sessions list, the Crew Mode filter, the crew member breadcrumb).
 *
 * Read it through `crewModeEnabled()` in `lib/crewMode.ts` (or `useCrewMode`
 * for a re-render), never as a bare flag: that one read is what every Crew Mode
 * surface consults.
 *
 * Default OFF, and OFF is the dashboard as it was before Crew Mode's UI: no
 * switch, no conductor hidden from the agent picker, and every session -- a crew
 * member included -- listed and alerting as an ordinary chat. The backend is
 * unchanged either way; without the UI nothing turns Crew Mode on. A chat that
 * is already in Crew Mode stays an ordinary chat on its conductor agent while
 * the flag is off, and its crew members are ordinary rows of the list.
 */
export const PREVIEW_CREW_MODE = `${PREVIEW_FLAG_PREFIX}crew-mode`

/**
 * A crewmate's sessions leave the person's own lists: every session a mate
 * thread dispatched (its `mate_root` stamp), at any depth, is left out of the
 * sidebar, Recents, the grid and unread counts, and shows on the mate's
 * Sessions tab instead. The mate's own chat stays; History and search still find
 * every session (step 6 of
 * docs/request-for-change/rfc-crewmate-workers-under-the-crewmate.md).
 *
 * Read it through `mateSessionsHidden()` in `lib/crewMode.ts`. Unlike the other
 * flags here it also has a server half: a person's `GET /api/chat/slots` leaves
 * the same rows out (`crew_mode.list_view`). Default OFF, and OFF is every list
 * as before, a mate's sessions included.
 */
export const PREVIEW_MATE_SESSIONS = `${PREVIEW_FLAG_PREFIX}mate-sessions`

/**
 * Explore: the top-bar "Explore N/7" pill and its phone twins (the bar ring and
 * the overflow-menu row), the Explore sheet, the milestone notice, the Try today
 * card on a new chat, and the Display > Explore switch.
 *
 * Read it through `useExplorePreview()` (`hooks/usePreviewFlag.ts`).
 *
 * Default OFF, because the sheet is still settling and its intro video carries
 * burned-in Chinese text that every other language would see. OFF draws none of
 * those surfaces and sends no milestones read from the new-chat page. With it
 * ON, `dashboard.milestones_enabled` stays the inner switch. The backend keeps
 * recording milestones either way: this gates the INGRESS only.
 */
export const PREVIEW_EXPLORE = `${PREVIEW_FLAG_PREFIX}explore`

/**
 * Crew-owned GitHub sign-in: the gateway signs in to GitHub itself and serves it
 * to KAS chats (`connections/crew_oauth.py`). Server-only: the gateway reads
 * `dashboard.feature_previews.crew_oauth` per call, so nothing in the dashboard
 * reads this key. Default OFF.
 */
export const PREVIEW_CREW_OAUTH = `${PREVIEW_FLAG_PREFIX}crew-oauth`

/**
 * Out-of-date Bot-written Dashboard fields: the page marks each bound field a
 * Bot wrote and has not rewritten within its limit (24 hours unless the field
 * says otherwise) "may be out of date", and the Bot's turn names those fields
 * so it rewrites them (`dashboard_agentic.stale_line`). Server-only: the
 * gateway reads `dashboard.feature_previews.dashboard_stale_fields` per read,
 * so nothing in the dashboard reads this key. Default OFF, and OFF is every
 * page and every turn as before.
 */
export const PREVIEW_DASHBOARD_STALE_FIELDS = `${PREVIEW_FLAG_PREFIX}dashboard-stale-fields`

/**
 * Agent Harness for worker chats: lets a conductor start a session on a harness it names
 * (`session_create(backend=...)`, `work_dispatch(backend=...)`). Server-only:
 * the gateway reads `dashboard.feature_previews.session_harness` per call
 * (`session_harness_preview.py`), so nothing in the dashboard reads this key.
 * Default OFF: the tools do not offer `backend` and refuse one. A Bot's own
 * harness setting and `spawn_run(backend=...)` work either way.
 */
export const PREVIEW_SESSION_HARNESS = `${PREVIEW_FLAG_PREFIX}session-harness`

/**
 * Wake a Bot for its out-of-date fields: when a field the Bot writes passes its
 * limit, the gateway starts one short turn in the Bot's own chat asking it to
 * write those fields again (`dashboard/stale_wake.py`), even with no check-in
 * on. Needs {@link PREVIEW_DASHBOARD_STALE_FIELDS}; its switch is shown only
 * while that one is on. Server-only, default OFF.
 */
export const PREVIEW_DASHBOARD_STALE_WAKE = `${PREVIEW_FLAG_PREFIX}dashboard-stale-wake`

/**
 * Every live preview-flag key, the ones the gateway keeps. Each one's name (see
 * {@link previewFlagName}) is a writable `dashboard.feature_previews.<name>` path
 * in the gateway config; `test_feature_previews_config.py` pins this list equal
 * to the gateway's `FEATURE_PREVIEW_FLAGS`, and `previewFlagServerSync.test.tsx`
 * pins it to every flag constant declared here. The retired
 * {@link PREVIEW_INSTANCE_SESSIONS} is left out: the gateway never keeps it.
 */
export const PREVIEW_FLAGS: readonly string[] = [
  PREVIEW_WEBHOOKS,
  PREVIEW_ARTIFACT_DEPLOY,
  PREVIEW_REMOTE_CREW_CHAT,
  PREVIEW_LAYOUT_HARNESS,
  PREVIEW_FOLDER_CONDUCTOR,
  PREVIEW_SESSION_ENVIRONMENTS,
  PREVIEW_CREW_MODE,
  PREVIEW_MATE_SESSIONS,
  PREVIEW_EXPLORE,
  PREVIEW_CREW_OAUTH,
  PREVIEW_DASHBOARD_STALE_FIELDS,
  PREVIEW_SESSION_HARNESS,
  PREVIEW_DASHBOARD_STALE_WAKE,
  PREVIEW_BRIDGE_APPROVALS,
]

/**
 * *flag*'s name in the gateway config: the key without
 * {@link PREVIEW_FLAG_PREFIX}, hyphens as underscores (config paths are
 * snake_case). `mc-preview-artifact-deploy` -> `artifact_deploy`.
 */
export function previewFlagName(flag: string): string {
  return flag.slice(PREVIEW_FLAG_PREFIX.length).replace(/-/g, '_')
}

/** The gateway config path holding *flag*: `dashboard.feature_previews.<name>`. */
export function previewFlagConfigPath(flag: string): string {
  return ['dashboard', 'feature_previews', previewFlagName(flag)].join('.')
}

/** Sends a toggled flag to the gateway; see {@link setPreviewFlagPersister}. */
export type PreviewFlagPersister = (flag: string, on: boolean) => void

let persister: PreviewFlagPersister | null = null

/**
 * Install (or, with `null`, remove) the writer {@link setPreviewFlag} hands each
 * toggle to after the cache write lands. Registered by the app shell's
 * `usePreviewFlagServerSync`; with none installed a toggle changes only the
 * cache, which is what a test that never mounts the shell gets.
 */
export function setPreviewFlagPersister(next: PreviewFlagPersister | null): void {
  persister = next
}

/**
 * Read a preview flag. Absent, unparseable, or storage-denied all mean OFF —
 * the whole point of the gate is that a surface stays hidden unless someone
 * deliberately turned it on, so it fails closed.
 *
 * An LTS build reads every flag as OFF whatever the browser holds (RFC D12), so
 * a key flipped through Developer > Storage or devtools turns nothing on.
 */
export function readPreviewFlag(flag: string): boolean {
  if (previewFlagsLocked()) return false
  return safeGetItem(flag) === '1'
}

/** Whether this build turns every Feature Preview off (the LTS edition). */
export function previewFlagsLocked(): boolean {
  return controlLocked(LOCKED_FEATURE_PREVIEWS)
}

/**
 * Turn "Chat on a crew" on or off. Off also clears the retired key, or that key
 * would keep the feature on behind a switch that reads off.
 */
export function setRemoteCrewChat(on: boolean): boolean {
  if (!setPreviewFlag(PREVIEW_REMOTE_CREW_CHAT, on)) return false
  if (!on && readPreviewFlag(PREVIEW_INSTANCE_SESSIONS)) return setPreviewFlag(PREVIEW_INSTANCE_SESSIONS, false)
  return true
}

/**
 * Write a preview flag to the browser cache, announce it, and hand it to the
 * installed persister so the gateway keeps it too.
 *
 * Returns whether the cache write actually landed. The announcement is gated on that
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
 * outcome: nothing was saved, and nothing is sent to the gateway. An LTS build
 * refuses every write the same way.
 */
export function setPreviewFlag(flag: string, on: boolean): boolean {
  if (!cachePreviewFlag(flag, on)) return false
  persister?.(flag, on)
  return true
}

/**
 * Write a preview flag to the browser cache only, and announce it, without
 * sending it to the gateway. For applying a value the gateway already holds;
 * a user's toggle goes through {@link setPreviewFlag}. Returns whether the
 * write landed, for the reason given there.
 */
export function cachePreviewFlag(flag: string, on: boolean): boolean {
  if (previewFlagsLocked()) return false
  if (!safeSetItem(flag, on ? '1' : '0')) return false
  const detail: PreviewFlagChange = { key: flag, on }
  window.dispatchEvent(new CustomEvent<PreviewFlagChange>(PREVIEW_FLAG_EVENT, { detail }))
  return true
}
