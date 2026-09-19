import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { ChevronRight, Circle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ChatSlot } from '../../types'
import { Btn } from '../../components/ui'
import { ROW_ACTIVE_CLS, ROW_IDLE_CLS } from '../../components/listShell'
import { cn } from '../../lib/utils'
import { tabStatus, type TabStatus } from '../../lib/sessionTabs'
import { timeAgo } from '../../utils/timeAgo'
import { lastActivityEpoch } from '../chat/sessionOrder'

const SESSION_STATUS: Record<TabStatus, { cls: string; text: string; label: string; spoken: boolean }> = {
  permission: { cls: 'fill-warn text-warn', text: 'text-warn', label: 'pages.chatSidebar.needs_approval', spoken: true },
  question: { cls: 'fill-info text-info', text: 'text-info', label: 'pages.chatSidebar.needs_your_answer', spoken: true },
  running: { cls: 'fill-ok text-ok', text: 'text-ok', label: 'pages.membersPage.drawer_working', spoken: false },
  unread: { cls: 'fill-accent text-accent', text: 'text-accent', label: 'pages.membersPage.unread_message', spoken: false },
  idle: { cls: 'fill-muted text-muted', text: 'text-muted', label: 'pages.membersPage.driving_idle', spoken: false },
}

/** Hold each member's row order through status updates so clicks keep their target. */
export function useMemberSessions(slots: readonly ChatSlot[]) {
  const previous = useRef(new Map<string, ChatSlot[]>())
  return useMemo(() => {
    const grouped = new Map<string, ChatSlot[]>()
    for (const slot of slots) {
      if (!slot.created_by) continue
      const children = grouped.get(slot.created_by) ?? []
      children.push(slot)
      grouped.set(slot.created_by, children)
    }
    for (const [owner, children] of grouped) {
      const byKey = new Map(children.map((slot) => [slot.key, slot]))
      const prior = previous.current.get(owner)
      const sameSet = prior?.length === children.length && prior.every((slot) => byKey.has(slot.key))
      grouped.set(owner, sameSet
        ? prior.map((slot) => byKey.get(slot.key)!)
        : children.sort((a, b) => lastActivityEpoch(b) - lastActivityEpoch(a)))
    }
    previous.current = grouped
    return grouped
  }, [slots])
}

export function MemberSessionRow({ session, selected, unreadSlots = [], onOpen, compact = false }: {
  session: ChatSlot
  selected: boolean
  unreadSlots?: string[]
  onOpen: (key: string) => void
  compact?: boolean
}) {
  const { t } = useTranslation()
  const kind = tabStatus(session, unreadSlots, session.key)
  const status = SESSION_STATUS[kind]
  const label = t(status.label)
  const title = session.title || session.key
  const activityTs = lastActivityEpoch(session)
  return (
    <Btn
      type="button"
      onClick={() => onOpen(session.key)}
      aria-current={selected ? 'true' : undefined}
      title={`${title} · ${label}`}
      className={cn(
        'w-full min-w-0 text-left border-0 shadow-none',
        compact ? 'text-[11px] px-1.5 py-1' : 'text-[13px] min-h-11 md:min-h-8 px-2 py-1.5',
        selected ? ROW_ACTIVE_CLS : ROW_IDLE_CLS,
      )}
      data-testid={compact ? 'member-driving-row' : 'member-session-row'}
      data-session-key={session.key}
      data-status={kind}
    >
      <Circle className={`lucide-inline !w-2 !h-2 shrink-0 ${status.cls}`} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate">{title}</span>
        {!compact && status.spoken && <span className={`block text-[11px] ${status.text}`}>{label}</span>}
      </span>
      {(compact || !status.spoken) && (
        <span className={status.spoken ? `shrink-0 font-medium ${status.text}` : 'sr-only'}>{label}</span>
      )}
      {compact && activityTs > 0 && (
        <span className="text-muted shrink-0 whitespace-nowrap">{timeAgo(activityTs)}</span>
      )}
    </Btn>
  )
}

export default function MemberSessions({ sessions, selectedKey, unreadSlots, onOpen }: {
  sessions: readonly ChatSlot[]
  selectedKey: string
  unreadSlots: string[]
  onOpen: (key: string) => void
}) {
  const { t } = useTranslation()
  const id = useId()
  const reduceMotion = useReducedMotion()
  const [expanded, setExpanded] = useState(true)
  // A deep link or a summary-row click must reveal its selected roster row.
  useEffect(() => { if (selectedKey) setExpanded(true) }, [selectedKey])
  return (
    <div className="ml-7 mr-1 mb-1 min-w-0" data-testid="member-sessions">
      <Btn
        type="button"
        aria-expanded={expanded}
        aria-controls={id}
        onClick={() => setExpanded((value) => !value)}
        className="w-full min-h-11 md:min-h-8 border-0 px-1.5 py-1 text-muted text-[12px]"
        data-testid="member-sessions-toggle"
      >
        <motion.span initial={false} animate={{ rotate: expanded ? 90 : 0 }} transition={{ duration: reduceMotion ? 0 : 0.15 }}>
          <ChevronRight className="lucide-inline" aria-hidden />
        </motion.span>
        {t('pages.chatSidebar.sessions')}
      </Btn>
      <motion.div
        id={id}
        initial={false}
        animate={{ height: expanded ? 'auto' : 0, opacity: expanded ? 1 : 0 }}
        transition={{ duration: reduceMotion ? 0 : 0.15 }}
        className="overflow-hidden"
        aria-hidden={!expanded}
        // @ts-expect-error React 18 types omit the native inert attribute.
        inert={!expanded ? '' : undefined}
      >
        <ul className="list-none m-0 pl-2 border-l border-border">
          {sessions.map((session) => (
            <li key={session.key}>
              <MemberSessionRow
                session={session}
                selected={selectedKey === session.key}
                unreadSlots={unreadSlots}
                onOpen={onOpen}
              />
            </li>
          ))}
        </ul>
      </motion.div>
    </div>
  )
}
