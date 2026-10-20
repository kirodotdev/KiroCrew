import { memo } from 'react'
import { X, MessageSquareText } from 'lucide-react'
import { useReviewComments, removeReviewComment } from '../store/reviewComments'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/**
 * Pending inline review comments, rendered above the composer (via
 * ChatInput's aboveComposer slot so it shares the composer's exact box
 * geometry). One chip per draft; X removes it. The drafts leave with the
 * next normal send: ChatPage attaches and clears them atomically.
 */
export default memo(function ReviewCommentBar({ slotId }: { slotId: string | null }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const drafts = useReviewComments(slotId)
  if (!slotId || drafts.length === 0) return null
  return (
    <div className="pb-1.5">
      <div className="rounded-xl border border-border bg-bg-elevated px-3 py-2 text-[12px]">
        <div className="flex items-center gap-1.5 text-muted pb-1">
          <MessageSquareText size={13} />
          <span>{i18nT('components.reviewCommentBar.pending_label', { count: drafts.length })}</span>
        </div>
        <div className="flex flex-col gap-1">
          {drafts.map(d => (
            <div key={d.id} className="flex items-center gap-2 min-w-0">
              <span className="font-mono text-muted shrink-0 max-w-[45%] truncate" title={d.file}>{d.file.split('/').pop()}:{d.line}{d.endLine != null && d.endLine !== d.line ? `-${d.endLine}` : ''}{d.side === 'old' ? ` (${i18nT('components.reviewCommentBar.old_side')})` : ''}</span>
              <span className="truncate flex-1 min-w-0">{d.text}</span>
              <button
                className="p-0.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer shrink-0"
                onClick={() => removeReviewComment(slotId, d.id)}
                title={i18nT('components.reviewCommentBar.remove')}
                aria-label={`${i18nT('components.reviewCommentBar.remove')} ${d.file}:${d.line}`}
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
})
