/**
 * Built-in surface registrations. Imported as a side-effect from `App.tsx`
 * (above the `getBuiltinSurfaces()` call that builds NAV_ITEMS) so that by
 * the time `App.tsx` evaluates, every static nav destination is already in
 * the registry.
 *
 * Order in this file = order in the rail (within each group). Add new
 * built-in surfaces here; do not add hardcoded badge logic to `App.tsx`.
 */
import { MessageSquare, Bell, BookOpen, Component, CalendarDays, Settings, ClipboardCheck, LayoutGrid, Webhook } from 'lucide-react'
import { createSelector } from '@reduxjs/toolkit'
import { KiroGhostMark } from '../components/KiroGhostMark'
import { registerBuiltinSurface, surfaceMachineValue } from './registry'
import { selectSubagentActivityCount } from '../store/chatSlice'
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
  // Cross-page "agents are working" dot. Kept OUT of the unread badge on
  // purpose: sub-agents in flight are not unread conversations, and folding
  // them into that count would corrupt both the number and the tab-title
  // attention sum.
  activitySelector: selectSubagentActivityCount,
  activityLabel: 'subagents in flight',
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
// POST /api/hooks/agent. A Main-group destination because it is an always-on
// gateway capability, not an installable app.
registerBuiltinSurface({
  navId: 'webhooks',
  route: '/webhooks',
  label: surfaceMachineValue('Webhooks'),
  labelKey: 'nav.webhooks',
  icon: <Webhook size={16} />,
  group: surfaceMachineValue('Main'),
})

// ── Apps ───────────────────────────────────────────────────────────────────
registerBuiltinSurface({
  navId: 'apps',
  route: '/apps',
  label: 'Explore',
  labelKey: 'nav.explore',
  icon: <LayoutGrid size={16} />,
  group: 'Apps',
  // Rendered by App.tsx as the accent link in the "Apps" section-header row
  // (expanded) / an icon row (collapsed) — not a regular rail list item.
  // Route, badge wiring, and onboarding anchor stay intact.
  hiddenFromNav: true,
})

// Instances (multi-instance management) is configured under Settings → Instances
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

// Knowledge sits in the Main group directly below Artifacts (order in this file
// = order in the rail within a group), keeping the two content surfaces
// adjacent and always-visible rather than tucked into the collapsible Apps group.
registerBuiltinSurface({
  navId: 'knowledge',
  route: '/knowledge',
  label: 'Knowledge',
  labelKey: 'nav.knowledge',
  icon: <BookOpen size={16} />,
  group: 'Main',
})

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
