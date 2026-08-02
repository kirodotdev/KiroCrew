// The dashboard registry — the ONE small shared file that ties a DashboardTab
// to its nav metadata and its view component. Adding a dashboard = add its
// view file (self-contained) + one entry here. Nothing else changes.
import { LayoutDashboard, Tags, type LucideIcon } from 'lucide-react'
import type { ComponentType } from 'react'
import type { DashboardTab } from '../lib/types'
import { i18nT } from '../../../i18n/t'
import OverviewView from './OverviewView'
import TaggingView from './TaggingView'

interface DashboardEntry {
  key: DashboardTab
  /** Nav label. A GETTER on each entry below — see the note there. */
  label: string
  icon: LucideIcon
  component: ComponentType
}

/**
 * The nav entries, in display order.
 *
 * `label` is a GETTER, not a value: this array is built once at module load, so a
 * plain `i18nT()` call here would resolve against whatever language was active at
 * boot and never re-resolve on a language switch. `DashboardsSection` reads
 * `d.label` while rendering, so a getter puts the lookup back on the render path
 * without changing the consumer or this entry's shape.
 */
export const DASHBOARDS: DashboardEntry[] = [
  {
    key: 'overview',
    get label() { return i18nT('apps.issueRadar.views.registry.overview') },
    icon: LayoutDashboard,
    component: OverviewView,
  },
  {
    key: 'tagging',
    get label() { return i18nT('apps.issueRadar.views.registry.tagging') },
    icon: Tags,
    component: TaggingView,
  },
]

export function dashboardComponent(tab: DashboardTab): ComponentType {
  return (DASHBOARDS.find((d) => d.key === tab) ?? DASHBOARDS[0]).component
}
