import { CheckCircle, Clock, Lock, Bot, X } from 'lucide-react'
import Clickable from '../../components/Clickable'
import type { Notification } from '../../types'

import { i18nT } from '../../i18n/t'
export default function NotificationItem({ n, active, onOpen, onDelete }: { n: Notification; active?: boolean; onOpen?: () => void; onDelete: (ts: string) => void }) {
  const acked = n.acked
  return (
    <Clickable
      className={`group p-2 px-2.5 rounded-md mb-1 text-[13px] border bg-card cursor-pointer transition-all animate-slide-in-left hover:border-border-strong hover:bg-bg-hover ${acked ? 'opacity-50' : ''} ${active ? 'border-accent bg-accent-subtle' : n.kind === 'approval' ? 'border-l-[3px] border-l-warn border-border' : n.kind === 'cron' ? 'border-l-[3px] border-l-accent border-border' : 'border-l-[3px] border-l-info border-border'}`}
      onClick={() => onOpen?.()}
      title={n.title}
    >
      <div className="font-semibold text-text-strong text-[13px] mb-0.5 flex items-start gap-1.5">
        <span className="shrink-0 mt-0.5">{acked ? <CheckCircle className="lucide-inline" /> : n.kind === 'cron' ? <Clock className="lucide-inline" /> : n.kind === 'approval' ? <Lock className="lucide-inline" /> : <Bot className="lucide-inline" />}</span>
        <span className="break-words line-clamp-2 min-w-0 flex-1">{n.title}</span>
        <button type="button" className="opacity-0 group-hover:opacity-40 cursor-pointer text-[12px] shrink-0 mt-0.5 hover:!opacity-100 hover:text-danger transition-opacity bg-transparent border-none p-0" aria-label={i18nT('pages.chat.notificationItem.delete')} onClick={(e) => { e.stopPropagation(); onDelete(n.ts) }}><X className="lucide-inline" /></button>
      </div>
      <div className="text-muted text-[12px] ml-[22px]">{acked ? i18nT('pages.chat.notificationItem.acknowledged') : (n.body || '').slice(0, 100)}{!acked && (n.body || '').length > 100 ? ' …' : ''}</div>
    </Clickable>
  )
}
