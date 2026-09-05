/**
 * ProcessDisclosure — "View process (N steps)" under a chat-profile reply.
 *
 * The chat projection hides a turn's tool rows, intermediate assistant text
 * and notices behind the final reply (`meta.chat_process`). This toggle lets
 * the reader open that trail in place: tool rows show their stamped purpose
 * (or the tool name), everything else its first line. Same collapse grammar
 * as TurnBlock's CollapseToggle / CollapsibleSection, kept local on purpose.
 */
import { type ReactNode } from 'react'
import { ChevronRight, Wrench } from 'lucide-react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import type { ChatMessage } from '../../types'
import { useRowDisclosure } from '../chat/rowDisclosure'
import { firstLine } from './ChatFoldRow'

function CollapseToggle({ expanded, onToggle, label }: { expanded: boolean; onToggle: () => void; label: string }) {
  return (
    <button
      type="button"
      className="flex items-center gap-2 text-[12px] leading-5 text-muted/60 hover:text-muted cursor-pointer bg-transparent border-none py-1 transition-colors"
      onClick={onToggle}
      aria-expanded={expanded}
      data-testid="process-disclosure-toggle"
    >
      <ChevronRight size={12} className={`transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} aria-hidden="true" />
      {label}
    </button>
  )
}

function CollapsibleSection({ expanded, children }: { expanded: boolean; children: ReactNode }) {
  return (
    <motion.div
      initial={false}
      animate={expanded ? { height: 'auto', opacity: 1 } : { height: 0, opacity: 0 }}
      transition={{ height: { duration: 0.3, ease: [0.4, 0, 0.2, 1] }, opacity: { duration: 0.2 } }}
      style={{ overflow: 'hidden' }}
      aria-hidden={!expanded}
    >
      <div className="shadow-[inset_2px_0_0_0_var(--border)] forced-colors:border-l-2 opacity-70">{children}</div>
    </motion.div>
  )
}

export default function ProcessDisclosure({ message }: { message: ChatMessage }) {
  const { t } = useTranslation()
  const meta = (message.meta ?? {}) as Record<string, unknown>
  const rows = Array.isArray(meta.chat_process) ? (meta.chat_process as ChatMessage[]) : []
  const count = typeof meta.chat_process_count === 'number' ? meta.chat_process_count : rows.filter(r => r.role === 'tool').length
  const mid = typeof meta.mid === 'string' ? 'process-' + meta.mid : undefined
  const [expanded, setExpanded] = useRowDisclosure(mid, false)
  if (!rows.length) return null

  return (
    <div className="w-full min-w-0 mt-0.5" data-testid="process-disclosure">
      <CollapseToggle
        expanded={expanded}
        onToggle={() => setExpanded(v => !v)}
        label={expanded ? t('pages.members.chat.hide_process') : t('pages.members.chat.view_process', { count })}
      />
      <CollapsibleSection expanded={expanded}>
        <ul className="list-none m-0 pl-4 py-1 flex flex-col gap-0.5" data-testid="process-disclosure-body">
          {rows.map((r, i) => {
            const isTool = r.role === 'tool'
            const line = firstLine(r)
            return (
              <li key={(r.ts ?? '') + '-' + i} className="flex items-baseline gap-2 text-[12px] leading-5 text-muted min-w-0">
                {isTool
                  ? <Wrench size={11} className="shrink-0 self-center" aria-hidden="true" />
                  : <span className="font-mono text-[11px] opacity-70 shrink-0">{r.role}</span>}
                <span className={`min-w-0 flex-1 truncate ${isTool ? 'font-mono text-[12px]' : ''}`}>{line}</span>
              </li>
            )
          })}
        </ul>
      </CollapsibleSection>
    </div>
  )
}
