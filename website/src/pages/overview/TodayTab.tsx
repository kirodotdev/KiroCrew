import { ArrowRight, CalendarDays } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Btn, Card, CardTitle, EmptyState, PanelSectionHeader } from '../../components/ui'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { useAppDispatch } from '../../store'
import { resumeFromHistory, switchSlot } from '../../store/chatSlice'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { fmtRelativeTime } from '../chat/sessionOrder'
import type { TodayGroup, TodaySession } from './todayActivity'
import { useTodayActivity } from './useTodayActivity'

/**
 * Settings > Overview > Today (`?view=today`).
 *
 * What was worked on today, without a model call: the sessions active today
 * grouped by folder (a click opens the session, live or archived), and the
 * entries the memory pipeline has already folded into today's history file.
 * The memory half lags the sessions half by design -- a session is
 * summarized only after the consolidation idle window -- so its empty state
 * names that window and points at the Memory tab, where "Summarize now"
 * already lives. This surface adds no second summarize control.
 */

function SessionRow({ session, onOpen }: { session: TodaySession; onOpen: (s: TodaySession) => void }) {
  return (
    <Clickable
      onClick={() => onOpen(session)}
      className="flex items-center gap-2.5 px-2 py-1.5 rounded-md cursor-pointer hover:bg-bg-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      data-testid="today-session-row"
      data-session-key={session.key}
    >
      {/* Running differs in SHAPE as well as color -- a filled dot with a ring
          against a hollow circle -- so the distinction survives color-blind
          viewing; the aria-label carries it to assistive tech. */}
      <span
        className={`w-2 h-2 rounded-full shrink-0 ${session.running ? 'bg-ok ring-2 ring-ok/40' : 'border border-border-strong'}`}
        role={session.running ? 'img' : undefined}
        aria-label={session.running ? i18nT('pages.overview.todayCard.running_label') : undefined}
        data-running={session.running ? 'true' : undefined}
      />
      <span className="flex-1 min-w-0 truncate text-[13px] text-text">{session.title}</span>
      <span className="shrink-0 text-[12px] text-muted tabular-nums">
        {session.messages != null && <>{i18nT('pages.overview.todayCard.messages', { count: session.messages })} · </>}
        {i18nT('pages.overview.todayCard.last_active', { time: fmtRelativeTime(session.activity) })}
      </span>
    </Clickable>
  )
}

function SessionGroup({ group, onOpen }: { group: TodayGroup; onOpen: (s: TodaySession) => void }) {
  return (
    <div className="flex flex-col gap-0.5" data-testid="today-session-group">
      <PanelSectionHeader
        label={group.name ?? i18nT('pages.overview.todayCard.unfiled')}
        count={group.sessions.length}
        className="px-2 pt-2 pb-1"
      />
      {group.sessions.map(s => <SessionRow key={s.key} session={s} onOpen={onOpen} />)}
    </div>
  )
}

export default function TodayTab({ onOpenMemory }: { onOpenMemory: () => void }) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const today = useTodayActivity()

  const openSession = (s: TodaySession) => {
    // Same two doors the command palette uses: a live slot is switched to, an
    // archived row is resumed into a slot first.
    if (s.live) void dispatch(switchSlot({ key: s.key, announceOnMissing: true }))
    else void dispatch(resumeFromHistory({ key: s.key, title: s.title }))
    navigate('/chat')
  }

  return (
    <div className="grid gap-3.5 grid-cols-2 max-[760px]:grid-cols-1" data-testid="today-tab">
      <Card>
        <CardTitle>
          {i18nT('pages.overview.todayCard.sessions_today')}
          <span className="text-muted font-mono text-[12px] tabular-nums">{fmtNumber(today.sessions.length)}</span>
          {today.running > 0 && (
            <span className="text-muted text-[12px] font-normal">· {i18nT('pages.overview.todayCard.running', { n: fmtNumber(today.running) })}</span>
          )}
        </CardTitle>
        {/* The basis is visible text here, not only the card's hover title:
            the Usage card beside it counts sessions STARTED today, and a
            keyboard or touch reader never sees a title attribute. */}
        <p className="m-0 mb-2 text-[12px] text-muted" data-testid="today-sessions-basis">
          {/* Scope sentence only: the card's second sentence compares this count
              with the Usage card, which is not on screen in the drill-in. */}
          {i18nT('pages.overview.todayCard.sessions_active_scope')}
        </p>
        {today.sessionsError ? (
          // askAgent on: a read of the archived-session list, nothing unsaved here.
          <ErrorNotice title={i18nT('pages.overview.todayCard.sessions_read_failed')} message={today.sessionsError.message} messageClassName="font-mono" askAgent testId="today-sessions-error" />
        ) : today.loading ? (
          <div className="skeleton h-24 rounded" />
        ) : today.groups.length === 0 ? (
          <EmptyState
            icon={<CalendarDays />}
            title={i18nT('pages.overview.todayCard.no_sessions_yet')}
            subtitle={i18nT('pages.overview.todayCard.no_sessions_body')}
            action={
              <Btn onClick={() => navigate('/chat')} data-testid="today-go-to-chat">
                {i18nT('pages.overview.todayCard.go_to_chat')} <ArrowRight size={12} />
              </Btn>
            }
            testId="today-sessions-empty"
          />
        ) : (
          <div className="flex flex-col gap-1">
            {today.groups.map(g => <SessionGroup key={g.folder_id || 'unfiled'} group={g} onOpen={openSession} />)}
          </div>
        )}
      </Card>

      <Card>
        <CardTitle>
          {i18nT('pages.overview.todayCard.in_memory')}
          <span className="text-muted font-mono text-[12px] tabular-nums">{fmtNumber(today.entries.length)}</span>
          <Btn
            onClick={onOpenMemory}
            className="ml-auto border-none bg-transparent px-0 py-0 text-[12px] font-medium text-accent hover:bg-transparent hover:underline"
            data-testid="today-open-memory"
          >
            {i18nT('pages.overview.todayCard.open_memory')} <ArrowRight size={12} />
          </Btn>
        </CardTitle>
        {today.historyError ? (
          <ErrorNotice title={i18nT('pages.overview.todayCard.memory_read_failed')} message={today.historyError.message} messageClassName="font-mono" askAgent testId="today-history-error" />
        ) : today.entries.length === 0 ? (
          <EmptyState
            icon={<CalendarDays />}
            title={i18nT('pages.overview.todayCard.memory_empty_title')}
            subtitle={i18nT('pages.overview.todayCard.memory_footnote', { count: today.idleHours })}
            testId="today-memory-empty"
          />
        ) : (
          <div className="flex flex-col gap-3">
            <ol className="flex flex-col gap-3 m-0 p-0 list-none">
              {today.entries.map((e, i) => (
                <li key={`${e.time}-${i}`} className="border-l-2 border-border pl-3" data-testid="today-memory-entry">
                  <div className="text-[11.5px] text-muted font-mono tabular-nums">{e.time}</div>
                  <div className="text-[13px] text-text whitespace-pre-wrap break-words">{e.text}</div>
                </li>
              ))}
            </ol>
            <div className="text-[12px] text-muted">
              {i18nT('pages.overview.todayCard.memory_footnote', { count: today.idleHours })}
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}
