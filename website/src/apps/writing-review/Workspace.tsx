// Two-column layout: rail (review history + new review button) on the
// left, detail (verdict summary + findings) on the right.
import { Plus, Settings } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useWritingReview } from './context'
import EmptyState from './components/EmptyState'
import NewReviewDialog from './components/NewReviewDialog'
import ReviewDetail from './components/ReviewDetail'
import ReviewList from './components/ReviewList'
import ScanProgress from './components/ScanProgress'
import SettingsPanel from './components/SettingsPanel'

export default function Workspace() {
  const {
    selectedReviewId,
    newReviewDialogOpen,
    openNewReviewDialog,
    settingsDialogOpen,
    openSettingsDialog,
    activeJobId,
  } = useWritingReview()

  return (
    <div className="flex h-full min-h-0">
      <aside className="w-[320px] shrink-0 border-r border-border flex flex-col min-h-0">
        <div className="p-3 border-b border-border flex items-center gap-2">
          <button
            type="button"
            onClick={openNewReviewDialog}
            disabled={activeJobId !== null}
            title={
              activeJobId !== null
                ? i18nT('apps.writingReview.workspace.scanInProgress')
                : undefined
            }
            className="flex-1 flex items-center gap-2 px-3 py-2 rounded-md bg-accent text-accent-fg text-[13px] font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:opacity-50"
          >
            <Plus className="lucide-inline" aria-hidden="true" />
            {i18nT('apps.writingReview.workspace.newReview')}
          </button>
          <button
            type="button"
            onClick={openSettingsDialog}
            className="shrink-0 p-2 rounded-md border border-border text-text hover:bg-bg-hover"
            aria-label={i18nT('apps.writingReview.workspace.openSettings')}
            title={i18nT('apps.writingReview.workspace.openSettings')}
          >
            <Settings className="lucide-inline" aria-hidden="true" />
          </button>
        </div>
        <ReviewList />
      </aside>
      <main className="flex-1 min-w-0 flex flex-col min-h-0">
        {activeJobId ? (
          <ScanProgress />
        ) : selectedReviewId ? (
          <ReviewDetail />
        ) : (
          <EmptyState />
        )}
      </main>
      {newReviewDialogOpen && <NewReviewDialog />}
      {settingsDialogOpen && <SettingsPanel />}
    </div>
  )
}
