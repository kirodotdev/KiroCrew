/**
 * Built-in surface registrations. Imported as a side-effect from `App.tsx`
 * (above the `getBuiltinSurfaces()` call that builds NAV_ITEMS) so that by
 * the time `App.tsx` evaluates, every static nav destination is already in
 * the registry.
 *
 * Order in this file = order in the rail (within each group). Add new
 * built-in surfaces here; do not add hardcoded badge logic to `App.tsx`.
 */
import { MessageSquare, Bell, Component, CalendarDays, Settings, ClipboardCheck, Compass, Webhook, BookOpen, Link2, Library, MessageSquareText, Workflow, ScrollText, Bot } from 'lucide-react'
import type { ReactElement } from 'react'
import { createSelector } from '@reduxjs/toolkit'
import { KiroGhostMark } from '../components/KiroGhostMark'
import { CrewMemberMark } from '../components/CrewMemberMark'
import { registerBuiltinSurface } from './registry'
import { BUILTIN_SURFACE_NAV, CAPABILITY_SUB_ITEM_NAV, capabilitySubItemNav } from './surfaceData'
import { selectSubagentActivityCount } from '../store/chatSlice'
import { isSilencedNote } from '../store/notificationsSlice'
import type { RootState } from '../store'

// Memoized at the source so `selectAllSurfacesAttention`'s per-dispatch
// invocation only re-runs the .filter().length when the items array changes
// reference (which is the standard Redux Toolkit pattern).
//
// Silenced and passive rows are excluded, matching the backend's own
// `_unread_count` and the rule the bell sheet's badge applies. This sum reaches
// the user as the browser-tab attention number, and a muted note counted here is
// a `(n)` in the title that no surface the user can open accounts for: the bell
// omits it and the feed keeps silenced rows behind the muted disclosure, so
// there is nothing to click that would clear it.
const selectUnacknowledgedNotificationCount = createSelector(
  (s: RootState) => s.notifications.items,
  items => items.filter(n => !n.acked && !isSilencedNote(n)).length,
)

// ── Main ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.chat,
  // Slot-bearing: default chat slots have surface === '' (or no mode set).
  icon: <MessageSquare size={16} />,
  badgeLabel: 'unread conversations',
  // Expanded-rail activity stays separate from unread attention: sub-agents
  // in flight are not unread conversations, and folding them into that count
  // would corrupt both the number and the tab-title attention sum. The
  // collapsed rail omits this context-poor signal; session rows identify the
  // specific work when the user opens Sessions.
  activitySelector: selectSubagentActivityCount,
  activityLabel: 'subagents in flight',
})

// Crewmates — one durable, pinned DM thread per crew member. Sits directly
// under Sessions: both are conversation surfaces, but here the primary object
// is a NAMED MEMBER rather than a task-shaped session. `slotMode: 'member'`
// claims the `member-<slug>` slots this page's threads live in, so their
// unread counts ride this rail item instead of leaking into Sessions
// (`isChatPageSurface` deliberately does not admit 'member').
//
// `previewFlag` (`PREVIEW_CREW`, in `surfaceData.ts`) because crew is not
// released yet: it is not advertised until the operator opts in at Settings >
// Developer > Feature Previews. The rail and Search Everywhere both read
// `getAdvertisedSurfaces()`, and the browser-tab attention count applies the
// gate inside `selectAllSurfacesAttention`. A guide that points at the page
// names that switch as its prerequisite (`PREVIEW_FLAG_ENABLERS`). The sidebar
// create menu's "Crewmates" entry reads PREVIEW_CREW only to decide whether it
// lands on `/members` or on the Settings card that turns the page on.
registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.members,
  icon: <CrewMemberMark />,
  badgeLabel: 'unread member threads',
})

registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.notifications,
  icon: <Bell size={16} />,
  // Non-slot: count comes from the notifications panel.
  unreadSelector: selectUnacknowledgedNotificationCount,
  badgeLabel: 'notifications',
  // Surfaced as the topbar bell (App.tsx NotificationsBellButton), not a rail
  // item. Route + badge + tab-title attention count stay wired via the
  // selectors above; only the left-rail entry is suppressed.
})

registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.projects,
  icon: <ClipboardCheck size={16} />,
  // Stub surface — no slotMode and no unreadSelector. The Projects badge
  // (global task-gate approval count) comes from a React Query result that
  // lives outside Redux; shell/nav/railBadges.ts mirrors it into `appBadges['projects']`
  // and `NavBadge` picks it up via the appBadges fallback. The label here
  // is what the fallback path's aria-label uses.
  badgeLabel: 'approvals needed',
})

registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.schedule,
  icon: <CalendarDays size={16} />,
})

// Inbound webhooks: token store, registered contexts, and run history for
// POST /api/hooks/agent. Carries BOTH gates because they answer different
// questions:
//
//   previewFlag    — WHETHER to advertise it at all. The page works but is not
//                    polished enough to release, so nothing surfaces it until
//                    the operator enables it in Settings > Developer > Feature Previews.
//   hiddenFromNav  — WHERE it lives once advertised. It is operator
//                    configuration touched once at setup, not a daily
//                    destination, and a top-level rail slot overstated it next
//                    to Sessions and Schedule, so it is reached from
//                    Settings → Webhooks instead.
//
// Because `hiddenFromNav` already drops the surface from `getBuiltinSurfaces()`,
// the rail and palette never see it and cannot apply the preview gate
// themselves. The two places that DO surface it — the Settings tab
// (`SettingsPage`) and the palette entry (`pagesProvider` EXTRA_PAGES) — read
// PREVIEW_WEBHOOKS directly, so the Settings > Developer > Feature Previews toggle still controls
// visibility end to end. Dropping `previewFlag` to release means dropping it in
// those two readers and the PREVIEW_SURFACES row too.
//
// The route stays registered either way, so a bookmark still resolves.
registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.webhooks,
  icon: <Webhook size={16} />,
})

// ── Apps ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.apps,
  // Renders as "Discover": `surfaceLabel()` resolves `labelKey` first, so the
  // legacy "Explore" `label` in surfaceData.ts is only the missing-catalog
  // fallback.
  icon: <Compass size={16} />,
  // Rendered by App.tsx as the accent link in the "Apps" section-header row
  // (expanded) / an icon row (collapsed) — not a regular rail list item.
  // Route, badge wiring, and onboarding anchor stay intact.
})

// Instances (multi-instance management) is configured under Settings → Remote Crew
// (after Browser, before Security) and switched via the top-header tab strip —
// it intentionally has no left-rail surface of its own.

registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.artifacts,
  icon: <Component size={16} />,
})

// Knowledge is not a main-rail surface BY DEFAULT: it lives as a tab inside
// Customize (CapabilitiesPage), grouped with Prompts and Steering —
// the other feed-the-agent assets. The old /knowledge route redirects there
// (App.tsx), so bookmarks and deep links keep resolving. It is registered
// below as `pinnable`, so a user who works in it daily can promote it onto the
// rail; absent that pin the rail is unchanged.

// ── Promotable sub-items (Customize panel) ─────────────────────────────────
// Each of these is a tab inside /capabilities. They are registered as real
// surfaces so a promoted row gets the rail's ordinary label/icon/active
// handling, `labelKey` resolution and test coverage — but `pinnable` keeps
// them off the rail until the user promotes one, so a default install renders
// exactly the rows it rendered before.
//
// `labelKey` deliberately reuses the SAME catalog keys CapabilitiesPage's own
// tab strip renders (`pages.capabilitiesPage.*_label`) rather than minting
// `nav.*` twins. The row and the tab it promotes are the same destination, so
// two keys would be two names for one thing and could drift apart per locale.
//
// `route` carries the panel's tab query param, which is how CapabilitiesPage
// already addresses its tabs (it does not opt into SidePanelLayout's
// path-based `basePath` mode). No new `<Route>` is needed: /capabilities is
// already routed and consumes `?tab=`.
//
// Icons are the tab's own glyph EXCEPT where that glyph is already spoken for
// on the rail, because a promoted row sits among the rail's rows rather than
// among its panel's tabs, and the collapsed rail is icon-only:
//   steering  Compass -> ScrollText  (Discover owns Compass, App.tsx, always rendered)
//   crews     Users   -> Bot         (see below)
// `crews` keeps Bot even though the rail no longer draws Users at all: Crew
// Members used to own that glyph, and now draws its own ghost-in-a-bubble brand
// mark (`components/CrewMemberMark.tsx`), so Users is free again. Bot stays
// because it is the better glyph on its own merits — a crew is a configured
// AGENT, where Users reads as a group of people — and reverting it would only
// re-spend review on a settled choice. The rule above is about collisions; this
// row simply no longer has one.
// `hooks` KEEPS its tab glyph. It was briefly moved to Zap because the
// palette's standalone /hooks entry drew a Webhook, but this change deletes that
// entry, and the `webhooks` surface is `hiddenFromNav` so it has no rail row --
// nothing owns Webhook on the rail, and the rule above then says keep the tab's.
// Sibling distinguishability on the rail beats matching the tab strip; the tab
// keeps its own glyph, which is what the panel's own rail needs.
//
// The tab, label key and English fallback of each row live in
// `surfaceData.ts` (`CAPABILITY_SUB_ITEM_NAV`); only the glyph is chosen here.
const CAPABILITY_SUB_ITEM_ICONS: Record<string, ReactElement> = {
  crews: <Bot size={16} />,
  skills: <BookOpen size={16} />,
  mcp: <Link2 size={16} />,
  knowledge: <Library size={16} />,
  prompts: <MessageSquareText size={16} />,
  steering: <ScrollText size={16} />,
  hooks: <Webhook size={16} />,
  workflows: <Workflow size={16} />,
}

for (const s of CAPABILITY_SUB_ITEM_NAV) {
  registerBuiltinSurface({ ...capabilitySubItemNav(s), icon: CAPABILITY_SUB_ITEM_ICONS[s.tab] })
}

// ── Bottom ─────────────────────────────────────────────────────────────────
// Agents + Capabilities merged into one bottom-pinned "Customize"
// destination. The /capabilities secondary panel hosts Crewmates (bindings),
// Custom agents, Connections, Skills, Hooks, and Prompts;
// /agents redirects there (see App.tsx routes).
//
// Icon: the Kiro ghost brand mark (not a Lucide glyph) — this row is the
// agent-identity destination, so it carries the mascot. `KiroGhostMark` paints
// the asset as a mask over `currentColor`, so it still follows the rail's
// active/idle colour states.
registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.capabilities,
  icon: <KiroGhostMark size={16} />,
})

registerBuiltinSurface({
  ...BUILTIN_SURFACE_NAV.settings,
  icon: <Settings size={16} />,
  // NOTE: the Settings nav dot (gateway update OR desktop update available)
  // is hand-rolled in App.tsx's bottom-fixed section, which renders this row
  // directly (not via renderNavRow/NavBadge) -- a registry badge here would
  // be dead code.
})
