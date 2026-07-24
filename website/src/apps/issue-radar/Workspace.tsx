// Issue Radar — three-column workspace shell.
//
//   ┌────────────┬─────────────┬──────────────────────────┐
//   │  LEFT RAIL  │ ISSUE LIST  │      ISSUE DETAIL        │
//   │ (accordion: │ (filtered   │  (metadata + body)       │
//   │  Dashboards │  by rail)   │                          │
//   │  / Filters) │             │                          │
//   └────────────┴─────────────┴──────────────────────────┘
//
// In 'dashboard' main view the list+detail split is replaced by a full-width
// dashboard page (Overview / Ranking / Insights / Duplicates), chosen from the
// registry. 'settings' shows the Settings page in the same area. The rail stays
// visible in every mode. All shared state comes from useIssueRadar(); this file
// owns only presentational layout (column resize).
import { useState } from 'react'
import { CircleDot } from 'lucide-react'
import { useIssueRadar } from './context'
import {
  loadListWidth, LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH,
} from './lib/format'
import LeftRail from './components/LeftRail'
import IssueList from './components/IssueList'
import IssueDetail from './components/IssueDetail'
import SettingsView from './views/SettingsView'
import { dashboardComponent } from './views/registry'

export default function Workspace() {
  const { mainView, dashboardTab, activeIssue } = useIssueRadar()
  const [listWidth, setListWidth] = useState<number>(loadListWidth)

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = listWidth
    let latest = startW
    const onMove = (ev: MouseEvent) => {
      latest = Math.min(MAX_LIST_WIDTH, Math.max(MIN_LIST_WIDTH, startW + ev.clientX - startX))
      setListWidth(latest)
    }
    const onUp = () => {
      localStorage.setItem(LIST_WIDTH_KEY, String(latest))
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const DashboardView = dashboardComponent(dashboardTab)

  return (
    <div className="flex h-full bg-bg text-text">
      <LeftRail />

      {mainView === 'issues' ? (
        <>
          <section style={{ width: listWidth }} className="flex-shrink-0 min-h-0">
            <IssueList />
          </section>

          {/* Drag handle — resize the issue-list column. */}
          <div
            onMouseDown={startResize}
            title="Drag to resize"
            className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activeIssue
              ? <IssueDetail issue={activeIssue} />
              : (
                <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                  <CircleDot size={28} className="opacity-40" />
                  <div className="text-xs">Select an issue to see its details.</div>
                </div>
              )}
          </main>
        </>
      ) : mainView === 'settings' ? (
        <main className="flex-1 min-w-0 min-h-0">
          <SettingsView />
        </main>
      ) : (
        <main className="flex-1 min-w-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
          <DashboardView />
        </main>
      )}
    </div>
  )
}
