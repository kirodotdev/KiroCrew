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
import { useState, useRef, useEffect } from 'react'
import { CircleDot, GitPullRequest } from 'lucide-react'
import { useIssueRadar } from './context'
import {
  loadListWidth, LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH,
} from './lib/format'
import LeftRail from './components/LeftRail'
import IssueList from './components/IssueList'
import IssueDetail from './components/IssueDetail'
import PrList from './components/PrList'
import PrDetail from './components/PrDetail'
import SettingsView from './views/SettingsView'
import { dashboardComponent } from './views/registry'
import { usePointerDrag } from '../../hooks/usePointerDrag'

export default function Workspace() {
  const { mainView, dashboardTab, activeIssue, activePull } = useIssueRadar()
  const [listWidth, setListWidth] = useState<number>(loadListWidth)

  const startWRef = useRef(0)
  const listDraggingRef = useRef(false)
  const listResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      startWRef.current = listWidth
      listDraggingRef.current = true
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    onMove: ({ dx }) => {
      setListWidth(Math.min(MAX_LIST_WIDTH, Math.max(MIN_LIST_WIDTH, startWRef.current + dx)))
    },
    onEnd: ({ dx }) => {
      listDraggingRef.current = false
      const finalW = Math.min(MAX_LIST_WIDTH, Math.max(MIN_LIST_WIDTH, startWRef.current + dx))
      localStorage.setItem(LIST_WIDTH_KEY, String(finalW))
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    },
  })
  // Unmount guard: onEnd can't fire if the component unmounts mid-drag
  // (setPointerCapture dies with the element), so restore the global body styles
  // here to avoid leaving the resize cursor / text-selection lock stuck.
  useEffect(() => () => {
    if (listDraggingRef.current) {
      listDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])

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
            {...listResize}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize list"
            title="Drag to resize"
            className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
            style={{ touchAction: 'none' }}
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activeIssue
              ? <IssueDetail issue={activeIssue} />
              : (
                <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                  <CircleDot size={26} strokeWidth={1.5} className="opacity-50" />
                  <div className="text-[13px]">Select an issue to see its details.</div>
                </div>
              )}
          </main>
        </>
      ) : mainView === 'settings' ? (
        <main className="flex-1 min-w-0 min-h-0">
          <SettingsView />
        </main>
      ) : mainView === 'pulls' ? (
        <>
          <section style={{ width: listWidth }} className="flex-shrink-0 min-h-0">
            <PrList />
          </section>

          {/* Drag handle — resize the PR-list column. */}
          <div
            {...listResize}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize list"
            title="Drag to resize"
            className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
            style={{ touchAction: 'none' }}
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activePull
              ? <PrDetail pull={activePull} />
              : (
                <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                  <GitPullRequest size={26} strokeWidth={1.5} className="opacity-50" />
                  <div className="text-[13px]">Select a Pull Request to see its details.</div>
                </div>
              )}
          </main>
        </>
      ) : (
        <main className="flex-1 min-w-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
          <DashboardView />
        </main>
      )}
    </div>
  )
}
