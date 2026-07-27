// The dashboard registry — the ONE small shared file that ties a DashboardTab
// to its nav metadata and its view component. Adding a dashboard = add its
// view file (self-contained) + one entry here. Nothing else changes.
import { LayoutDashboard, Tags, Sparkles, BarChart3, CopyCheck, type LucideIcon } from 'lucide-react'
import type { ComponentType } from 'react'
import type { DashboardTab } from '../lib/types'
import OverviewView from './OverviewView'
import TaggingView from './TaggingView'
import RankingView from './RankingView'
import InsightsView from './InsightsView'
import DuplicatesView from './DuplicatesView'

interface DashboardEntry {
  key: DashboardTab
  label: string
  icon: LucideIcon
  /** Nav still routes to it, but it renders a "coming soon" placeholder. */
  soon?: boolean
  component: ComponentType
}

export const DASHBOARDS: DashboardEntry[] = [
  { key: 'overview', label: 'Overview', icon: LayoutDashboard, component: OverviewView },
  { key: 'tagging', label: 'Tagging', icon: Tags, component: TaggingView },
  { key: 'ranking', label: 'Ranking', icon: Sparkles, soon: true, component: RankingView },
  { key: 'insights', label: 'Insights', icon: BarChart3, soon: true, component: InsightsView },
  { key: 'duplicates', label: 'Duplicates', icon: CopyCheck, soon: true, component: DuplicatesView },
]

export function dashboardComponent(tab: DashboardTab): ComponentType {
  return (DASHBOARDS.find((d) => d.key === tab) ?? DASHBOARDS[0]).component
}
