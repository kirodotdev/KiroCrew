/** One line under a conductor's latest finished reply: how many of its workers
 *  need the person, have stalled, run or wait. A click opens the side panel on
 *  Workers. It is for a reader who cannot see the sidebar's conductor row
 *  (sidebar closed, a phone, split view), so it says what that row says, from
 *  the same data (`useCrewRootAttention`).
 *
 *  Drawn only when all hold: the Feature Preview is on, the chat is a
 *  conductor mode root and not a Bot's chat, this row is the newest reply that
 *  draws a footer (`latestFooterReply`), and some worker needs the person,
 *  stalled, runs or waits. A failed worker read says so in the line's place.
 *  An older reply never draws it, so a live count is never read as "the state
 *  back then". The single chat page and the split-view panes mount it; a Bot's
 *  chat pane and the app SDK's bubble (Bot pages) do not. */
import { memo, useMemo } from 'react'
import { ChevronRight } from 'lucide-react'
import { shallowEqual } from 'react-redux'
import { useAppSelector } from '../../store'
import type { ChatMessage } from '../../types'
import { i18nT } from '../../i18n/t'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { PREVIEW_CREW_REPLY_STATUS } from '../../utils/previewFlags'
import { crewModeRoot } from '../../lib/crewMode'
import { isMemberThreadSlot } from '../../hooks/unreadOnAttention'
import { latestFooterReply } from '../../pages/chat/replyFooter'
import { CrewRowAttention } from './CrewRowCounts'
import { useCrewRootAttention } from './useCrewRootAttention'
import { requestCrewPanelList } from '../../pages/chat/command-center/crewPanelView'
import ErrorNotice from '../ErrorNotice'

interface CrewReplyStatusProps {
  /** The chat the reply belongs to. */
  slotKey: string | undefined
  /** The reply row this line would sit under. */
  message: ChatMessage
  /** A split-view pane's own rows and running state. Absent on the single
   *  chat page, which reads the active chat's rows from the store. */
  messages?: readonly ChatMessage[]
  running?: boolean
  /** Offer "ask the agent" on a failed read. A host that would lose local
   *  state by navigating (a split pane) turns it off, as its other notices do. */
  askAgent?: boolean
  /** Open the side panel on Workers for *slot*. */
  onOpenWorkers: (slot: string) => void
}

/** Whether *row* is *message*: the same object, or the same reply by its time. */
function sameReply(row: ChatMessage | null, message: ChatMessage): boolean {
  if (!row) return false
  return row === message || (!!message.ts && row.ts === message.ts && row.role === message.role)
}

export const CrewReplyStatus = memo(function CrewReplyStatus({ slotKey, message, messages, running, askAgent = true, onOpenWorkers }: CrewReplyStatusProps) {
  // Its words are translated: a language switch reaches a memo boundary only through this.
  useLanguageGeneration()
  const on = usePreviewFlag(PREVIEW_CREW_REPLY_STATUS)
  // A pane hands its own rows; the page's rows are the store's active chat.
  const paneLatest = useMemo(
    () => (on && messages ? sameReply(latestFooterReply(messages, !!running), message) : null),
    [on, messages, running, message],
  )
  // One boolean per row: only the row whose answer flips re-renders.
  const latest = useAppSelector(st => on && !!slotKey && (paneLatest !== null ? paneLatest
    : st.chat.activeSlot === slotKey && sameReply(latestFooterReply(st.chat.messages, st.chat.slotRunning), message)))
  const root = useAppSelector(st => {
    if (!latest || !slotKey) return null
    const slot = st.dashboard.slots.find(x => x.key === slotKey)
    // A conductor root only (the slot list holds this gateway's own rows). A
    // Bot's chat (a member thread, or a slot running a member) is left out.
    if (!slot || !crewModeRoot(slot)) return null
    if (slot.agent_kind === 'member' || isMemberThreadSlot(slotKey, st.dashboard.slots)) return null
    return { work: slot.crew_work ?? null, owes: slot.pending_approval || slot.needs_input ? 1 : 0 }
  }, shallowEqual)
  if (!root || !slotKey) return null
  return <CrewReplyStatusLine root={slotKey} work={root.work} rootOwes={root.owes} askAgent={askAgent} onOpenWorkers={onOpenWorkers} />
})

/** The line itself. Split out so only the one latest reply reads the worker
 *  list; every other reply stops at the cheap checks above. */
function CrewReplyStatusLine({ root, work, rootOwes, askAgent, onOpenWorkers }: {
  root: string
  askAgent: boolean
  work: Parameters<typeof useCrewRootAttention>[1]
  rootOwes: number
  onOpenWorkers: (slot: string) => void
}) {
  const a = useCrewRootAttention(root, work, rootOwes)
  // A failed read says so, as the sidebar row does: the reader may not see that row.
  if (a.failed) {
    return (
      <div className="self-start mt-0.5 px-1 max-w-full min-w-0 text-[12px]" data-testid="crew-reply-status-error">
        <ErrorNotice variant="inline" askAgent={askAgent} className="min-w-0" messageClassName="truncate" messageTooltip={i18nT('commandCenter.workers_error')} message={i18nT('commandCenter.workers_error')} />
      </div>
    )
  }
  // With nothing moving the line goes; there is no "all done" text.
  if (a.needsYou + a.stalled + a.running + a.waiting === 0) return null
  return (
    <button
      type="button"
      className="self-start mt-1 inline-flex items-center gap-2 max-w-full min-w-0 px-2 py-1 rounded-md border border-border text-left text-muted cursor-pointer hover:bg-bg-hover hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      data-testid="crew-reply-status"
      aria-label={i18nT('components.crewMode.reply_status_label', { status: a.title })}
      title={i18nT('components.crewMode.reply_status_open')}
      onClick={() => { requestCrewPanelList(root); onOpenWorkers(root) }}
    >
      <CrewRowAttention needsYou={a.needsYou} stalled={a.stalled} running={a.running} waiting={a.waiting} done={false} paused={a.paused}
        stalledName={a.stalled > 0 ? a.stalledName : undefined} stalledForMs={a.stalled > 0 ? a.stalledForMs : undefined} title={a.title} />
      {/* What a click does, in words: the counts alone read as plain text. */}
      <span aria-hidden="true" className="shrink-0 inline-flex items-center gap-0.5 text-[12px] text-accent whitespace-nowrap" data-testid="crew-reply-status-open">
        {i18nT('components.crewMode.conductor_mode_view_list')}<ChevronRight size={12} className="shrink-0" />
      </span>
    </button>
  )
}
