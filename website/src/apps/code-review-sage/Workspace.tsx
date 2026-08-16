// The shell: rail | detail. Two columns, not three.
//
// The lists (pull requests / reviews) and the repo picker are stacked in the
// rail; everything to the right of it is the report you are reading. A third
// column spent a fixed slice of the window on a list you had already used to get
// where you are — and when no repo was selected it held nothing but an empty
// state pointing back at the rail.
//
// Reports are the widest content in the app (finding bodies, diffs, check
// tables), so the space belongs to them.
import { ScanSearch } from 'lucide-react'

import { useColumnResize } from '../../hooks/useColumnResize'
import EmptyState from './components/EmptyState'
import LeftRail from './components/LeftRail'
import PrReviewDetail from './components/PrReviewDetail'
import RunDetail from './components/RunDetail'
import { useSage } from './context'
import {
  MAX_RAIL_WIDTH, MIN_RAIL_WIDTH, RAIL_WIDTH_KEY, loadRailWidth,
} from './lib/layout'
import LearningView from './views/LearningView'
import SettingsView from './views/SettingsView'

import { i18nT } from '../../i18n/t'
/** The 6px vertical drag handle between two columns. */
function Splitter({ handleProps, label }: {
  handleProps: ReturnType<typeof useColumnResize>['handleProps']
  label: string
}) {
  return (
    <div
      {...handleProps}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={i18nT('apps.codeReviewSage.workspace.drag_to_resize')}
      className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
      style={{ touchAction: 'none' }}
    />
  )
}

export default function Workspace() {
  const { mainView, activeRun, selectedPr } = useSage()

  const rail = useColumnResize(RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH)

  return (
    // overflow-hidden so a mis-sized child can never grow the shell past the
    // viewport and push the rail's identity footer below the fold — each column
    // owns its own scrolling.
    <div className="flex h-full overflow-hidden bg-bg text-text">
      <div style={{ width: rail.width }} className="flex-shrink-0 min-h-0 flex">
        <LeftRail />
      </div>
      <Splitter handleProps={rail.handleProps} label={i18nT('apps.codeReviewSage.workspace.resize_sidebar')} />

      {mainView === 'reviews' ? (
        <>

          {/* A flex COLUMN, not just a flex item: EmptyState (and any future
              child) sizes itself with flex-1, which is inert unless this element
              is itself a flex container — that bug left the empty state
              collapsed to content height and pinned to the top of the pane. */}
          <main className="flex-1 min-w-0 min-h-0 flex flex-col">
            {selectedPr ? (
              <PrReviewDetail pr={selectedPr} />
            ) : activeRun ? (
              <RunDetail run={activeRun} />
            ) : (
              <EmptyState
                icon={ScanSearch}
                title={i18nT('apps.codeReviewSage.workspace.select_a_review_to_see_its_progress_and_report')}
                hint={i18nT('apps.codeReviewSage.workspace.start_a_new_one_several_can_run_at_once')}
              />
            )}
          </main>
        </>
      ) : mainView === 'learning' ? (
        <main className="flex-1 min-w-0 min-h-0 flex flex-col"><LearningView /></main>
      ) : (
        <main className="flex-1 min-w-0 min-h-0 flex flex-col"><SettingsView /></main>
      )}
    </div>
  )
}
