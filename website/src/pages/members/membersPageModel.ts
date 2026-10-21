/**
 * The Crewmates page's pure rules: which crewmate a visit lands on
 * (`resolveMateLanding`, `resolveDefaultMember`), the Profile memory reading
 * (`memberMemoryDisplay` and its catalog keys), the side panel's placement
 * arithmetic (`panelSitsBeside`, `panelChrome`), the views the panel
 * withholds, the create-link parser, the `?member=` param and the roster's
 * width bounds. Nothing here needs React, so each rule is tested directly; the
 * `MembersPage` facade re-exports the ones its importers use. A constant one
 * owner reads lives with that owner, not here.
 */
import { type CreatedCrewmate, type CrewmateDraft } from './NewCrewmateDialog'
import { useTranslation } from 'react-i18next'
import { type MemberRosterRow } from '../../api/client'
import { isAssistantMember, pendingMate } from '../../lib/assistantMember'
import { type ProfileTab } from './CrewProfilePanel'
import { SIDE_PANEL_MIN_W, SIDE_PANEL_RESERVED_W, type SidePanelWithholdable } from '../chat/SidePanel'
import { CHAT_TRANSCRIPT_VIEWS, VIEW_DATA_SOURCE, type ViewKind } from '../../hooks/usePanelTabs'
import { loadColumnWidth } from '../../lib/columnWidth'

/** The open member rides the URL (`?member=<name>`) so a reload keeps it
 *  and a link lands on one. Switching members REPLACES the entry — the page
 *  holds one history entry, so Back leaves it in one press (the Sessions
 *  sidebar's rule); only the below-md roster->thread step pushes. The value
 *  is the exact crew NAME, not the slug: the slug is lossy (see the header
 *  comment of MembersPage.tsx), and a link that resolved `Oncall` to `oncall`'s thread would be
 *  the silent misroute this page exists to prevent. */
export const MEMBER_PARAM = 'member'

/** Which crewmate to open when the URL names none and this browser holds no
 *  usable memory (the restore effect looks `LAST_MEMBER_KEY` up first), or to
 *  fall back to when the URL names one that is gone (deleted or renamed since
 *  the link was written): the conversation holding the newest message,
 *  whoever sent it -- the greatest `last_active_ts` (the crew log's fold, so
 *  it survives a gateway restart and a new browser alike), strict `>` so a tie
 *  keeps the first in `ordered`, which is also the answer when no row carries
 *  a stamp. Like a messages app. Product decision (CrewMates launch review):
 *  when crewmates exist and none is selected, a conversation opens by default;
 *  the "pick one" landing is gone. The built-in `default` crewmate is a row
 *  like any other here; a roster holding only `default` lands on the
 *  empty-state hero (`hasNoCrewmates`, guarded by the restore effect) before
 *  this is asked. `undefined` only for an empty `ordered`. Pure, so the cases
 *  -- newest, tie, no stamps, empty -- are tested directly.
 *  Mate's first visit is decided before this, by {@link resolveMateLanding}. */
export function resolveDefaultMember(ordered: readonly MemberRosterRow[]): MemberRosterRow | undefined {
  let best: MemberRosterRow | undefined
  for (const m of ordered) {
    if (!best || (m.last_active_ts ?? 0) > (best.last_active_ts ?? 0)) best = m
  }
  return best
}

/** Mate (`isAssistantMember`) while its welcome is still coming: the Crewmates
 *  page lands there first, ahead of a remembered or more recently used
 *  crewmate, so every user, fresh or existing, is introduced to it once
 *  (rfc-mate-landing-once). Still coming = the welcome is owed
 *  (`welcome_owed`), its once-only marker is not claimed (`welcome_claimed`),
 *  and its thread holds nothing (`pendingMate`). Anything else counts as seen:
 *  a welcome that was claimed and then failed leaves the thread empty for
 *  good, and one never owed is never sent, so neither may pin the user on
 *  Mate. A row that does not say (an unreadable marker, an older gateway)
 *  counts as seen too. Then this answers `undefined` and the
 *  page's usual landing rules apply unchanged. A separate, early rule on
 *  purpose, so the usual rules ({@link resolveDefaultMember}) carry no Mate
 *  special case. */
export function resolveMateLanding(rows: readonly MemberRosterRow[]): MemberRosterRow | undefined {
  const mate = pendingMate(rows)
  return mate && mate.welcome_owed === true && mate.welcome_claimed === false ? mate : undefined
}

type MemberMemoryDisplay = 'global' | 'legacy' | 'private' | 'ownership_mismatch' | 'unavailable'

/** Full catalog keys for the Profile Memory tile, one per `memberMemoryDisplay` answer. */
export const PROFILE_MEMORY_KEYS: Record<MemberMemoryDisplay, string> = {
  private: 'pages.membersPage.profile_memory_private',
  global: 'pages.membersPage.profile_memory_global',
  legacy: 'pages.membersPage.profile_memory_legacy',
  ownership_mismatch: 'pages.membersPage.profile_memory_ownership_mismatch',
  unavailable: 'pages.membersPage.profile_memory_unavailable',
}

export function memberMemoryDisplay(row: MemberRosterRow): MemberMemoryDisplay {
  if (row.name === 'default') return row.memory_store === 'default' ? 'global' : 'unavailable'
  if (isAssistantMember(row) && row.memory_store === 'default' && !row.memory_owner) return 'global'
  if (row.memory_owner && row.memory_owner !== row.name) return 'ownership_mismatch'
  if (row.memory_version === 2) return row.memory_owner === row.name ? 'private' : 'unavailable'
  if (row.memory_version === 1 && !row.memory_owner) return 'legacy'
  return 'unavailable'
}

/** Roster width bounds, persisted like the chat sidebar's (mc-sidebar-width). */
export const ROSTER_MIN = 200
export const ROSTER_MAX = 420
const ROSTER_DEFAULT = 264
export const ROSTER_WIDTH_KEY = 'mc-members-roster-width'
export const CREW_DASHBOARD_TAB_ID = 'crew-dashboard'
/** Standing host tabs in SidePanel. Files comes from the pinned view block;
 * Dashboard is the one host-owned leading tab. Profile is a sibling surface. */
export const CREW_PANEL_TAB_IDS: readonly string[] = [CREW_DASHBOARD_TAB_ID]
/** Chat-panel views this page withholds from the strip and the + menu
 *  (`SidePanel.hiddenViews`). The unfed half is DERIVED, not enumerated: every
 *  view `VIEW_DATA_SOURCE` classifies as `chat-transcript` (Changes / Issues /
 *  Links / Pins today) reads indexes ChatPage builds over the transcript, none
 *  of which runs here, so each would render an affirmative "none" — and a new
 *  transcript-fed view must be classified where kinds are defined before it can
 *  exist, so it cannot arrive here unwithheld. `summary` is withheld by choice:
 *  Profile Sessions and the generated Dashboard are the member-level views; the
 *  generic chat session summary would be a third, unrelated summary. Exported so the test pins the set. */
export const MEMBERS_UNFED_VIEWS: readonly ViewKind[] = [...CHAT_TRANSCRIPT_VIEWS, 'summary']
/** Everything this page withholds once the thread is confirmed. The rule: the
 *  STANDING tabs here are Dashboard (the host-owned leading tab) and Files (the
 *  one pinned view this page keeps); everything the chat page reaches through
 *  the + menu stays reachable through the + menu here too (Side Chat, Terminal,
 *  Browser, Git, Subagents, Workflows, the Developer-Mode views and any
 *  app-contributed tab), bound to the member's own slot. Withheld, then: the
 *  unfed views above, the chat page's Command Center (the task dashboard lives
 *  in the permanent Dashboard tab, not a second chat view) and Artifacts — a
 *  PINNED standing tab on the chat page, and this page's standing set is
 *  Dashboard + Files, so it is the one withdrawal versus the plain chat panel.
 *  Withdrawing the Artifacts LIST does not withdraw an artifact DOCUMENT: a
 *  `/artifacts/<slug>` link in a crewmate's reply opens its own tab here
 *  (`onArtifactOpen` → `openArtifactGuarded`), and `SidePanel.isWithheld`
 *  ties document tabs to `'files'`, which this page withholds only while the
 *  thread is unconfirmed (#18320). Side chat IS offered — its composer draft lives in the chat-core store
 *  (`sideChatDrafts`, per slot, persisted), so `SidePanel` unmounting the body
 *  on a tab or member switch loses nothing, and the selection toolbar's "Ask
 *  about this" needs the tab as its landing (`openMemberSideChat`); it stays
 *  dynamic, never standing. Exported so the test pins the set. */
export const MEMBERS_WITHHELD_VIEWS: readonly SidePanelWithholdable[] = [...MEMBERS_UNFED_VIEWS, 'command-center', 'artifacts']
/** Everything the panel withholds while the thread is UNCONFIRMED: every
 *  classified view, plus Terminal and app tabs. Derived from
 *  `VIEW_DATA_SOURCE` (the exhaustive `Record<ViewKind, …>`) rather than
 *  enumerated, so the same guarantee the unfed set has holds here too — a new
 *  `ViewKind` cannot arrive in this window offered; it is withheld by
 *  construction until the slot it would bind to exists. Exported so the test
 *  pins the set against the classification. */
export const MEMBERS_UNCONFIRMED_WITHHELD_VIEWS: readonly SidePanelWithholdable[] = [
  ...(Object.keys(VIEW_DATA_SOURCE) as ViewKind[]), 'terminal', 'app',
]
/** This page's three inter-column gap-2s (24px) — space the side panel must
 *  keep clear beside the roster so a drag can never fold the thread to zero.
 *  The thread's own minimum is already inside the panel's shell reserve
 *  (`SIDE_PANEL_RESERVED_W` budgets the nav rail plus a chat-pane minimum). */
export const PANEL_GAPS_W = 24
/** Strip of chat the phone roster drawer leaves uncovered beside it — the same
 *  40px the chat page's sessions drawer leaves, so the two drawers match. */
export const ROSTER_DRAWER_UNCOVERED_PX = 40
/** Whether the side panel can sit BESIDE the thread as a permanent column,
 *  or must become an overlay the user opens. Beside needs the shell reserve
 *  (nav rail + a usable thread) plus the live roster width plus the panel's
 *  own minimum — the same arithmetic the chat page's `sidePanelFillWidth` does
 *  for its two columns, with the roster added. Pure, so the boundary is
 *  tested directly. Mobile always overlays (its viewport seats neither). */
export function panelSitsBeside({ winW, rosterW, isMobile }: { winW: number; rosterW: number; isMobile: boolean }): boolean {
  if (isMobile) return false
  return winW - rosterW - PANEL_GAPS_W >= SIDE_PANEL_RESERVED_W + SIDE_PANEL_MIN_W
}
/** Whether the panel is on screen, and whether the header shows its opener —
 *  the two answers the placement and the two visibility flags decide together.
 *
 *  Each placement reads its OWN flag: the docked column reads the persisted
 *  `dockedOpen`, the overlay reads the per-visit `overlayOpen`. The opener
 *  disappears while the docked column is open, because the open panel's own
 *  strip carries the close control there — the chat page's split of the gesture
 *  across the two halves. As an overlay the opener always stays, since a
 *  dismissed drawer has no strip left to reopen it from.
 *
 *  Pure, so both placements and all four flag combinations are tested directly. */
export function panelChrome({ beside, dockedOpen, overlayOpen }: {
  beside: boolean
  dockedOpen: boolean
  overlayOpen: boolean
}): { panelVisible: boolean; showOpener: boolean } {
  return {
    panelVisible: beside ? dockedOpen : overlayOpen,
    showOpener: !beside || !dockedOpen,
  }
}
// Module-level so the resize hook's memoised resolver isn't invalidated every render.
export const loadRosterWidth = () => loadColumnWidth(ROSTER_WIDTH_KEY, ROSTER_MIN, ROSTER_MAX, ROSTER_DEFAULT)

/** i18n translate function, taken from the hook so the row need not re-derive
 *  its type. */
export type TFn = ReturnType<typeof useTranslation>['t']

/** A same-origin `/members?create=1` link's draft, or null for anything else
 *  -- including an href the URL parser refuses, which must never throw. */
export function createDraftFromHref(href: string | null, base: string): CrewmateDraft | null {
  if (!href) return null
  let url: URL
  try {
    url = new URL(href, base)
  } catch {
    return null
  }
  let origin: string
  try {
    origin = new URL(base).origin
  } catch {
    return null
  }
  if (url.origin !== origin || url.pathname !== '/members' || url.searchParams.get('create') !== '1') return null
  return { name: url.searchParams.get('name') ?? '', goal: url.searchParams.get('goal') ?? '' }
}
/** The Profile card while it is open, as the page holds it (`profile`). */
export interface ProfileCardOpen {
  tab: ProfileTab
  placement: 'column' | 'floating'
  openNonce: number
}

/** A link named a crewmate that is gone, and who opened instead (`gone`). */
export interface GoneNotice {
  name: string
  shown: string
}

/** A step after a successful create that failed (`postCreateError`). */
export interface PostCreateFailure {
  kind: 'roster'
  created: CreatedCrewmate
  // `stay`: the user had already chosen another chat when the create
  // landed, so the retry only re-reads the roster and opens nothing.
  stay?: boolean
}

