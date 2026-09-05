import { GitMerge } from 'lucide-react'
import { i18nT } from '../i18n/t'

/**
 * MergedReadonlyBar — the compact, non-writable footer a merged fork shows
 * WHERE THE COMPOSER WOULD BE (#3816 UX review).
 *
 * A merged fork's transcript stays readable, but every turn/mutation endpoint
 * answers 409 `{code: "session_merged"}`. A fully-enabled composer therefore
 * lies: the user types, hits send, and the send is rejected with nothing on
 * screen having warned them. This bar replaces the composer with a plain
 * statement that the session was merged, plus an "Open parent" button when the
 * fork's parent key is known — the one useful action from a dead-end fork.
 *
 * Rendered by BOTH surfaces that host a live composer: the single-chat
 * ChatPage and the split-view ChatPane. `onOpenParent` is omitted (button
 * hidden) when the parent key is unavailable, rather than rendering a button
 * that navigates nowhere.
 */
export default function MergedReadonlyBar({ onOpenParent }: { onOpenParent?: () => void }) {
  return (
    <div
      className="mx-auto w-full px-4 pb-3"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
      data-testid="merged-readonly-bar"
    >
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-3 py-2.5 text-[13px]">
        <GitMerge size={14} className="lucide-inline shrink-0 text-accent" aria-hidden="true" />
        <span className="flex-1 min-w-0 text-muted">
          {i18nT('components.chatPane.merged_readonly_bar')}
        </span>
        {onOpenParent && (
          <button
            type="button"
            onClick={onOpenParent}
            className="shrink-0 text-[12px] font-semibold text-accent bg-accent/10 hover:bg-accent/20 rounded px-2 py-1 cursor-pointer border-none transition-colors"
          >
            {i18nT('components.chatPane.merged_open_parent')}
          </button>
        )}
      </div>
    </div>
  )
}
