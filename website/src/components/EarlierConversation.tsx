import { ChevronDown, ChevronRight } from 'lucide-react'
import { formatTs, type MessageRenderer } from '../app-sdk/messageRenderers'
import { i18nT } from '../i18n/t'
import type { ChatMessage } from '../types'

/** Role of the one display-only row that marks a fresh start. Never stored:
 *  it is spliced into the list the pane draws, not into the transcript. */
export const EARLIER_CONVERSATION_ROLE = 'earlier_conversation'

/** True for a row written before *foldBefore* (ISO time). A row without a
 *  readable time is treated as new: it is most likely a live row. */
function isEarlier(m: ChatMessage, foldBefore: number): boolean {
  const at = m.ts ? Date.parse(m.ts) : NaN
  return Number.isFinite(at) && at < foldBefore
}

/**
 * The list to draw after a fresh start: the rows from before it fold under one
 * divider row. Closed, only the divider and the new rows are drawn; open, the
 * earlier rows come back above the divider. Returns *messages* itself when
 * there is nothing to fold.
 */
export function foldEarlierConversation(
  messages: ChatMessage[],
  foldBefore: string | undefined,
  open: boolean,
): ChatMessage[] {
  const at = foldBefore ? Date.parse(foldBefore) : NaN
  if (!Number.isFinite(at)) return messages
  const earlier: ChatMessage[] = []
  const later: ChatMessage[] = []
  for (const m of messages) (isEarlier(m, at) ? earlier : later).push(m)
  if (earlier.length === 0) return messages
  const divider: ChatMessage = {
    role: EARLIER_CONVERSATION_ROLE,
    content: '',
    cls: '',
    meta: { count: earlier.length, at: foldBefore },
  }
  return open ? [...earlier, divider, ...later] : [divider, ...later]
}

/** The divider row: a full-width rule with a toggle that opens the fold.
 *  It says when the fresh start happened, so the fold reads as the result
 *  of the press, and names its action: show (with the count) or hide. */
export function earlierConversationRenderer(open: boolean, onToggle: () => void): MessageRenderer {
  return {
    id: 'earlier-conversation',
    roles: [EARLIER_CONVERSATION_ROLE],
    render: (m, ctx) => ctx.row(
      <div className="my-3 flex items-center gap-2" data-testid="earlier-conversation">
        <span aria-hidden="true" className="h-px flex-1 bg-border" />
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[12px] text-muted bg-transparent border-none cursor-pointer hover:text-text focus-ring transition-colors"
          data-testid="earlier-conversation-toggle"
        >
          {open
            ? <ChevronDown size={13} aria-hidden="true" />
            : <ChevronRight size={13} aria-hidden="true" />}
          {(() => {
            const meta = m.meta as { count?: number; at?: string } | undefined
            const time = formatTs(meta?.at) ?? ''
            return open
              ? i18nT('components.chatPane.earlier_conversation_hide', { time })
              : i18nT('components.chatPane.earlier_conversation_show', { time, count: meta?.count ?? 0 })
          })()}
        </button>
        <span aria-hidden="true" className="h-px flex-1 bg-border" />
      </div>
    ),
  }
}
