/**
 * Navigation identity of every built-in surface, as pure data.
 *
 * `builtins.tsx` registers each surface by spreading the matching entry here and
 * adding what only a running dashboard has (icon element, badge and activity
 * selectors). The find_ui index generator (`scripts/gen-ui-index.mjs`) reads the
 * same entries without importing React or the store, so the rail, Search
 * Everywhere and the agent's location index cannot name a surface three ways.
 *
 * Only route, label key, group and the advertising flags live here. A field the
 * registry documents (`appOnly`, `hiddenFromNav`, `previewFlag`, `pinnable`)
 * means exactly what `registry.ts` says it means.
 */
import { PREVIEW_CREW, PREVIEW_WEBHOOKS } from '../utils/previewFlags'

/** Sidebar group bucket; `registry.ts` re-exports it as `SurfaceGroup`. */
export type SurfaceGroup = 'Main' | 'Apps' | 'Platform' | 'Bottom'

/**
 * Marks a registry literal as a machine value rather than rendered copy.
 *
 * Keep this wrapper narrow: `label` fallbacks in the built-in registry are
 * unreachable when `labelKey` is present (enforced by navLabels.test.tsx), and
 * `group` is a routing bucket. Other surface strings such as `badgeLabel` and
 * `activityLabel` are rendered and must not use this helper.
 */
export function surfaceMachineValue<T extends string>(value: T): T {
  return value
}

/** The data half of a `Surface` (see `registry.ts` for each field's contract). */
export interface SurfaceNavData {
  navId: string
  route: string
  label: string
  labelKey: string
  group: SurfaceGroup
  slotMode?: string
  appOnly?: boolean
  hiddenFromNav?: boolean
  previewFlag?: string
  pinnable?: boolean
}

/** One built-in surface per key, keyed by `navId`. Order = rail order. */
export const BUILTIN_SURFACE_NAV = {
  chat: {
    navId: 'chat', route: '/chat', label: surfaceMachineValue('Sessions'), labelKey: 'nav.sessions',
    group: surfaceMachineValue('Main'), slotMode: '',
  },
  members: {
    navId: 'members', route: '/members', label: surfaceMachineValue('Crewmates'), labelKey: 'nav.crew_members',
    group: surfaceMachineValue('Main'), slotMode: 'member', previewFlag: PREVIEW_CREW,
  },
  notifications: {
    navId: 'notifications', route: '/notifications', label: surfaceMachineValue('Notifications'),
    labelKey: 'nav.notifications', group: surfaceMachineValue('Main'), hiddenFromNav: true,
  },
  projects: {
    navId: 'projects', route: '/projects', label: surfaceMachineValue('Task Runner'), labelKey: 'nav.task_runner',
    group: surfaceMachineValue('Apps'), appOnly: true,
  },
  schedule: {
    navId: 'schedule', route: '/schedule', label: surfaceMachineValue('Schedule'), labelKey: 'nav.schedule',
    group: surfaceMachineValue('Main'),
  },
  webhooks: {
    navId: 'webhooks', route: '/webhooks', label: surfaceMachineValue('Webhooks'), labelKey: 'nav.webhooks',
    group: surfaceMachineValue('Main'), previewFlag: PREVIEW_WEBHOOKS, hiddenFromNav: true,
  },
  apps: {
    navId: 'apps', route: '/apps', label: surfaceMachineValue('Explore'), labelKey: 'nav.discover',
    group: surfaceMachineValue('Apps'), hiddenFromNav: true,
  },
  artifacts: {
    navId: 'artifacts', route: '/artifacts', label: surfaceMachineValue('Artifacts'), labelKey: 'nav.artifacts',
    group: surfaceMachineValue('Main'),
  },
  capabilities: {
    navId: 'capabilities', route: '/capabilities', label: surfaceMachineValue('Customize'),
    labelKey: 'nav.agent_capabilities', group: surfaceMachineValue('Bottom'),
  },
  settings: {
    navId: 'settings', route: '/settings', label: surfaceMachineValue('Settings'), labelKey: 'nav.settings',
    group: surfaceMachineValue('Bottom'),
  },
} as const satisfies Record<string, SurfaceNavData>

/**
 * Customize-panel tabs a user may promote onto the rail (`pinnable` surfaces).
 *
 * `labelKey` reuses the tab strip's own catalog keys
 * (`pages.capabilitiesPage.*_label`), so the promoted row and the tab it
 * promotes cannot drift apart per locale.
 */
export const CAPABILITY_SUB_ITEM_NAV: readonly { tab: string; labelKey: string; label: string }[] = [
  { tab: 'crews', labelKey: 'pages.capabilitiesPage.crews_label', label: surfaceMachineValue('Crews') },
  { tab: 'skills', labelKey: 'pages.capabilitiesPage.skills_label', label: surfaceMachineValue('Skills') },
  { tab: 'mcp', labelKey: 'pages.capabilitiesPage.connections_label', label: surfaceMachineValue('Connections') },
  { tab: 'knowledge', labelKey: 'pages.capabilitiesPage.knowledge_label', label: surfaceMachineValue('Knowledge') },
  { tab: 'prompts', labelKey: 'pages.capabilitiesPage.prompts_label', label: surfaceMachineValue('Prompts') },
  { tab: 'steering', labelKey: 'pages.capabilitiesPage.steering_label', label: surfaceMachineValue('Steering files') },
  { tab: 'hooks', labelKey: 'pages.capabilitiesPage.hooks_label', label: surfaceMachineValue('Hooks') },
  { tab: 'workflows', labelKey: 'pages.capabilitiesPage.workflows_label', label: surfaceMachineValue('Workflows') },
]

/** The pinnable surface for one Customize tab, as `builtins.tsx` registers it. */
export function capabilitySubItemNav(s: { tab: string; labelKey: string; label: string }): SurfaceNavData {
  return {
    navId: `capabilities-${s.tab}`,
    route: `/capabilities?tab=${s.tab}`,
    label: s.label,
    labelKey: s.labelKey,
    group: surfaceMachineValue('Main'),
    pinnable: true,
  }
}
