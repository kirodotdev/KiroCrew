// The dashboard registry — the ONE small shared file that ties a DashboardTab
// to its nav metadata and its view component. Adding a dashboard = add its
// view file (self-contained) + one entry here. Nothing else changes.
import { LayoutDashboard, Tags, type LucideIcon } from 'lucide-react'
import type { ComponentType } from 'react'
import type { DashboardTab } from '../lib/types'
import OverviewView from './OverviewView'
import TaggingView from './TaggingView'

interface DashboardEntry {
  key: DashboardTab
  label: string
  icon: LucideIcon
  component: ComponentType
}

export const DASHBOARDS: DashboardEntry[] = [
  { key: 'overview', label: 'Overview', icon: LayoutDashboard, component: OverviewView },
  { key: 'tagging', label: 'Tagging', icon: Tags, component: TaggingView },
]

export function dashboardComponent(tab: DashboardTab): ComponentType {
  return (DASHBOARDS.find((d) => d.key === tab) ?? DASHBOARDS[0]).component
}
