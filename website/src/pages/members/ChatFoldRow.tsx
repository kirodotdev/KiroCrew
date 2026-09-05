/**
 * ChatFoldRow — "N check-ins with nothing new".
 *
 * Draws the synthetic `chat_fold` row the chat projection emits for a run of
 * quiet nudge rounds. Collapsed it is one muted line; expanded it lists the
 * folded rows compactly (role + first line) so the reader can confirm nothing
 * was missed without leaving the chat profile.
 */
import { ChevronRight, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ChatMessage } from '../../types'
import { useRowDisclosure } from '../chat/rowDisclosure'
import { fmtMessageTime } from '../chat/messageTime'

export function firstLine(m: ChatMessage): string {
  const purpose = typeof m.meta?.purpose === 'string' ? m.meta.purpose : ''
  const src = purpose || (m.content ?? '')
  return src.replace(/^🔧\s*/, '').split('\n').find(l => l.trim().length > 0)?.trim() ?? ''
}

export default function ChatFoldRow({ message }: { message: ChatMessage }) {
  const { t } = useTranslation()
  const meta = (message.meta ?? {}) as Record<string, unknown>
  const count = typeof meta.count === 'number' ? meta.count : 0
  const rows = Array.isArray(meta.rows) ? (meta.rows as ChatMessage[]) : []
  const mid = typeof meta.mid === 'string' ? meta.mid : undefined
  const [expanded, setExpanded] = useRowDisclosure(mid, false)

  return (
    <div className="w-full min-w-0 text-muted" data-testid="chat-fold-row" data-count={count}>
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        aria-label={expanded ? t('pages.members.chat.hide_rounds') : t('pages.members.chat.show_rounds')}
        className="flex items-center gap-2 text-[12px] leading-5 text-muted/70 hover:text-muted cursor-pointer bg-transparent border-none py-1 transition-colors min-w-0 max-w-full"
        data-testid="chat-fold-toggle"
      >
        <ChevronRight size={12} className={`shrink-0 transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} aria-hidden="true" />
        <RefreshCw size={11} className="shrink-0" aria-hidden="true" />
        <span className="truncate">{t('pages.members.chat.silent_rounds', { count })}</span>
      </button>
      {expanded && (
        <ul className="list-none m-0 pl-6 pb-1 flex flex-col gap-0.5 shadow-[inset_2px_0_0_0_var(--border)] forced-colors:border-l-2" data-testid="chat-fold-body">
          {rows.map((r, i) => {
            const line = firstLine(r)
            return (
              <li key={(r.ts ?? '') + '-' + i} className="flex items-baseline gap-2 text-[11px] leading-4 min-w-0">
                <span className="font-mono opacity-70 shrink-0">{r.role}</span>
                <span className="truncate min-w-0 flex-1">{line}</span>
                {r.ts && <span className="opacity-60 shrink-0 tabular-nums">{fmtMessageTime(r.ts)}</span>}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
