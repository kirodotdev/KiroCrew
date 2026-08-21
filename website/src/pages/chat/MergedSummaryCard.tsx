import { memo } from 'react'
import { GitMerge } from 'lucide-react'
import type { ChatMessage } from '../../types'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'

/**
 * Inline card for a ``merged_summary`` row — the visible block the merge-back
 * endpoint appends to a parent when a fork is folded back in (issue #3816).
 *
 * A dedicated card (rather than letting the row fall through to the assistant
 * markdown renderer) is what makes the merge legible: the reader sees at a
 * glance that this is imported context — which fork it came from, that it is a
 * summary rather than the fork's raw turns, and any gap note about the parent
 * having advanced since the fork point. The body is the summarizer's markdown,
 * already redacted server-side, rendered through the same MarkdownRenderer as a
 * normal reply so links and code blocks behave identically.
 */
export default memo(function MergedSummaryCard({
  message,
  onFileOpen,
  onFolderOpen,
  slotKey,
}: {
  message: ChatMessage
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  onFolderOpen?: (path: string) => void
  slotKey?: string
}) {
  const meta = message.meta ?? {}
  const forkTitle = (meta.merged_from_title as string) || ''
  // Localized note from the structured count. There is no persisted-string
  // fallback: the backend writes ``advanced`` under exactly the condition a
  // note is warranted, so no shipped block can lack it (First Principles
  // review — a fallback would bake backend English into a 12-language
  // transcript to support a past that never existed).
  const advanced = typeof meta.advanced === 'number' && meta.advanced > 0 ? meta.advanced : 0
  const gapNote = advanced > 0
    ? i18nT('pages.chat.mergedSummaryCard.gap_note', { count: advanced })
    : ''
  // A head-fork's summary describes the copied parent prefix too; the backend
  // flags that so the header can say so rather than implying post-fork only.
  const coversFullFork = meta.covers_full_fork === true

  return (
    <div
      className="text-[13px] leading-5 rounded-md ring-1 ring-inset forced-colors:border ring-accent/20 bg-accent/5 px-3 py-2.5"
      data-testid="merged-summary-card"
    >
      <div className="flex items-center gap-2 text-accent font-semibold mb-1.5">
        <GitMerge size={14} className="lucide-inline shrink-0" aria-hidden="true" />
        <span className="truncate">
          {forkTitle
            ? i18nT('pages.chat.mergedSummaryCard.merged_from_fork', { name: forkTitle })
            : i18nT('pages.chat.mergedSummaryCard.merged_from_a_fork')}
        </span>
      </div>
      <p className="text-muted text-[11px] mb-2">
        {coversFullFork
          ? i18nT('pages.chat.mergedSummaryCard.summary_covers_full_fork')
          : i18nT('pages.chat.mergedSummaryCard.summary_of_forks_work')}
      </p>
      <MarkdownRenderer
        content={message.content}
        onFileOpen={onFileOpen}
        onFolderOpen={onFolderOpen}
        slotKey={slotKey}
      />
      {gapNote && <p className="text-muted text-[11px] mt-2 italic">{gapNote}</p>}
    </div>
  )
})
