// Placeholder shown in the detail pane when no review is selected.
import { PenLine } from 'lucide-react'

import { i18nT } from '../../../i18n/t'

export default function EmptyState() {
  return (
    <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-2.5 text-center px-6">
      <PenLine className="lucide-inline text-muted opacity-50" aria-hidden="true" style={{ fontSize: '28px' }} />
      <div className="text-[13px] text-text">
        {i18nT('apps.writingReview.emptyState.title')}
      </div>
      <div className="text-[11.5px] text-muted opacity-70">
        {i18nT('apps.writingReview.emptyState.hint')}
      </div>
    </div>
  )
}
