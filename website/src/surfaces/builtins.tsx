/**
 * Built-in surface registrations. Imported as a side-effect from `App.tsx`
 * (above the `getBuiltinSurfaces()` call that builds NAV_ITEMS) so that by
 * the time `App.tsx` evaluates, every static nav destination is already in
 * the registry.
 *
 * Order in this file = order in the rail (within each group). Add new
 * built-in surfaces here; do not add hardcoded badge logic to `App.tsx`.
 */
import { MessageSquare, Bell, Component, CalendarDays, Settings, ClipboardCheck, Compass, Webhook, Users } from 'lucide-react'
import { createSelector } from '@reduxjs/toolkit'
import { KiroGhostMark } from '../components/KiroGhostMark'
import { registerBuiltinSurface, surfaceMachineValue } from './registry'
import { selectSubagentActivityCount } from '../store/chatSlice'
import { PREVIEW_CREW, PREVIEW_WEBHOOKS } from '../utils/previewFlags'
import type { RootState } from '../store'

// Memoized at the source so `selectAllSurfacesAttention`'s per-dispatch
// invocation only re-runs the .filter().length when the items array changes
// reference (which is the standard Redux Toolkit pattern).
const selectUnacknowledgedNotificationCount = createSelector(
  (s: RootState) => s.notifications.items,
  items => items.filter(n => !n.acked).length,
)

// ── Main ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'chat',
  route: '/chat',
  label: 'Sessions',
  labelKey: 'nav.sessions',
  icon: <MessageSquare size={16} />,
  group: 'Main',
  // Slot-bearing: default chat slots have surface === '' (or no mode set).
  slotMode: '',
  badgeLabel: 'unread conversations',
  // Expanded-rail activity stays separate from unread attention: sub-agents
  // in flight are not unread conversations, and folding them into that count
  // would corrupt both the number and the tab-title attention sum. The
  // collapsed rail omits this context-poor signal; session rows identify the
  // specific work when the user opens Sessions.
  activitySelector: selectSubagentActivityCount,
  activityLabel: 'subagents in flight',
})

// Crew Members — one durable, pinned DM thread per crew member. Sits directly
// under Sessions: both are conversation surfaces, but here the primary object
// is a NAMED MEMBER rather than a task-shaped session. `slotMode: 'member'`
// claims the `member-<slug>` slots this page's threads live in, so their
// unread counts ride this rail item instead of leaking into Sessions
// (`isChatPageSurface` deliberately does not admit 'member').
//
// `previewFlag` because crew is not released yet: the page errors out on paths
// that are still being built, so it is not advertised until the operator opts in
// at Developer > Feature Previews. Unlike Webhooks below this surface is NOT
// `hiddenFromNav` — the rail IS where it belongs once released, so dropping the
// flag is the whole release. Two of the three advertising paths apply the gate
// for themselves — the rail and Search Everywhere both read
// `getAdvertisedSurfaces()` — and the third, the browser-tab attention count,
// applies it inside `selectAllSurfacesAttention`, because that sum reads the
// registry directly rather than the advertised list. The other door into crew
// (the sidebar's "New Crew Mode chat" entry) reads PREVIEW_CREW directly, since
// a create-menu item is not a surface at all.
registerBuiltinSurface({
  navId: 'members',
  route: '/members',
  label: surfaceMachineValue('Crew Members'),
  labelKey: 'nav.crew_members',
  icon: <Users size={16} />,
  group: surfaceMachineValue('Main'),
  slotMode: 'member',
  badgeLabel: 'unread member threads',
  previewFlag: PREVIEW_CREW,
})

registerBuiltinSurface({
  navId: 'notifications',
  route: '/notifications',
  label: 'Notifications',
  labelKey: 'nav.notifications',
  icon: <Bell size={16} />,
  group: 'Main',
  // Non-slot: count comes from the notifications panel.
  unreadSelector: selectUnacknowledgedNotificationCount,
  badgeLabel: 'notifications',
  // Surfaced as the topbar bell (App.tsx NotificationsBellButton), not a rail
  // item. Route + badge + tab-title attention count stay wired via the
  // selectors above; only the left-rail entry is suppressed.
  hiddenFromNav: true,
})

registerBuiltinSurface({
  navId: 'projects',
  route: '/projects',
  label: 'Task Runner',
  labelKey: 'nav.task_runner',
  icon: <ClipboardCheck size={16} />,
  group: 'Apps',
  appOnly: true,
  // Stub surface — no slotMode and no unreadSelector. The Projects badge
  // (global task-gate approval count) comes from a React Query result that
  // lives outside Redux; App.tsx mirrors it into `appBadges['projects']`
  // and `NavBadge` picks it up via the appBadges fallback. The label here
  // is what the fallback path's aria-label uses.
  badgeLabel: 'approvals needed',
})

registerBuiltinSurface({
  navId: 'schedule',
  route: '/schedule',
  label: 'Schedule',
  labelKey: 'nav.schedule',
  icon: <CalendarDays size={16} />,
  group: 'Main',
})

// Inbound webhooks: token store, registered contexts, and run history for
// POST /api/hooks/agent. Carries BOTH gates because they answer different
// questions:
//
//   previewFlag    — WHETHER to advertise it at all. The page works but is not
//                    polished enough to release, so nothing surfaces it until
//                    the operator enables it in Developer > Feature Previews.
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
// PREVIEW_WEBHOOKS directly, so the Developer > Feature Previews toggle still controls
// visibility end to end. Dropping `previewFlag` to release means dropping it in
// those two readers and the PREVIEW_SURFACES row too.
//
// The route stays registered either way, so a bookmark still resolves.
registerBuiltinSurface({
  navId: 'webhooks',
  route: '/webhooks',
  label: surfaceMachineValue('Webhooks'),
  labelKey: 'nav.webhooks',
  icon: <Webhook size={16} />,
  group: surfaceMachineValue('Main'),
  previewFlag: PREVIEW_WEBHOOKS,
  hiddenFromNav: true,
})

// ── Apps ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'apps',
  route: '/apps',
  label: 'Explore',
  // Renders as "Discover": `surfaceLabel()` resolves `labelKey` first, so the
  // legacy `label` above is only the missing-catalog fallback (kept verbatim —
  // a required field whose English value the i18n literal gate freezes).
  labelKey: 'nav.discover',
  icon: <Compass size={16} />,
  group: 'Apps',
  // Rendered by App.tsx as the accent link in the "Apps" section-header row
  // (expanded) / an icon row (collapsed) — not a regular rail list item.
  // Route, badge wiring, and onboarding anchor stay intact.
  hiddenFromNav: true,
})

// Instances (multi-instance management) is configured under Settings → Remote Instances
// (after Browser, before Security) and switched via the top-header tab strip —
// it intentionally has no left-rail surface of its own.

registerBuiltinSurface({
  navId: 'artifacts',
  route: '/artifacts',
  label: 'Artifacts',
  labelKey: 'nav.artifacts',
  icon: <Component size={16} />,
  group: 'Main',
})

// Knowledge is NOT a main-rail surface: it lives as a tab inside Agent
// Capabilities (CapabilitiesPage), grouped with Prompts and Steering — the
// other feed-the-agent assets. The old /knowledge route redirects there
// (App.tsx), so bookmarks and deep links keep resolving.

// ── Bottom ─────────────────────────────────────────────────────────────────
// Agents + Capabilities merged into one bottom-pinned "Agent Capabilities"
// destination. The /capabilities secondary panel hosts Crews (bindings),
// Agent Templates, Connections, Skills, Hooks, and Prompts;
// /agents redirects there (see App.tsx routes).
//
// Icon: the Kiro ghost brand mark (not a Lucide glyph) — this row is the
// agent-identity destination, so it carries the mascot. `KiroGhostMark` paints
// the asset as a mask over `currentColor`, so it still follows the rail's
// active/idle colour states.
registerBuiltinSurface({
  navId: 'capabilities',
  route: '/capabilities',
  label: 'Agent Capabilities',
  labelKey: 'nav.agent_capabilities',
  icon: <KiroGhostMark size={16} />,
  group: 'Bottom',
})

registerBuiltinSurface({
  navId: 'settings',
  route: '/settings',
  label: 'Settings',
  labelKey: 'nav.settings',
  icon: <Settings size={16} />,
  group: 'Bottom',
  // NOTE: the Settings nav dot (gateway update OR desktop update available)
  // is hand-rolled in App.tsx's bottom-fixed section, which renders this row
  // directly (not via renderNavRow/NavBadge) -- a registry badge here would
  // be dead code.
})
