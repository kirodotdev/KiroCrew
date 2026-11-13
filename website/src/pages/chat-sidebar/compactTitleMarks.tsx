/** A title-only session card's title-line marks. Title-only cards hide the
 *  agent, metadata and secondary lines, so a session that needs the person, or
 *  a crew that works, keeps one mark in front of its title, with the glyph and
 *  label the full card's line already uses. `SessionRow` places them. */
import type { ReactNode } from 'react'
import { Bot, Goal, Hand, MessageCircleQuestionMark, Pause, ShieldCheck, TriangleAlert, Zap } from 'lucide-react'
import MonitorRadar from '../../components/MonitorRadar'
import { i18nT } from '../../i18n/t'
import type { CrewWorkFacts } from '../../types'
import { useCrewWorkerSummary } from '../chat/command-center/CrewWorkerList'
import { codeChannelCrewMark } from './CodeChannelCrewRow'
import type { CrewState } from './codeChannelCrew'
import { useCrewRootNeedsYou } from './CrewRootSummaryLine'
import { summarizeCrewWorkers, crewRowCounts } from '../../lib/crewWorkers'
import ErrorNotice from '../../components/ErrorNotice'

export interface CompactMark { key: string; label: string; glyph: ReactNode }

/** The row's ONE title-line mark for its winning row state (never a new stacked
 *  line). Order: the row's own states first, in resolver order, because they
 *  describe this session; then a collapsed conductor's hidden-subtree ask; then
 *  unread last, because an owed answer anywhere in the subtree matters more than
 *  a result waiting to be read. Live work keeps the live dot; a stalled goal
 *  loop swaps the dot for its static danger glyph; every other branch leaves the
 *  title bare. A Crew Mode root's crew ask is `CompactCrewMarks`'. */
export function compactAttentionMark(a: {
  key: string | undefined; crewState: CrewState | null | undefined; iconPx: number
  subagentLabel: string; needsInputLabel: string; goalLoop: boolean; goalLoopStalled: boolean
  monitorBlocked: boolean; monitorLabel: string; crewRunningText: string; subtreeNeedsYou: boolean; unread: boolean
}): CompactMark | null {
  const px = a.iconPx
  if (a.key === 'code_channel_crew' && a.crewState) return { key: 'code_channel_crew', ...codeChannelCrewMark(a.crewState, px) }
  if (a.key === 'pending_approval') return { key: a.key, label: i18nT('pages.chatSidebar.needs_approval'), glyph: <ShieldCheck size={px} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden /> }
  if (a.key === 'subagent_awaiting') return { key: a.key, label: a.subagentLabel, glyph: <Bot size={px} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden /> }
  if (a.key === 'needs_input') return { key: a.key, label: a.needsInputLabel, glyph: <MessageCircleQuestionMark size={px} className="shrink-0" style={{ color: 'var(--info)' }} aria-hidden /> }
  if (a.key === 'loop_waiting') {
    return {
      key: a.key,
      label: i18nT('pages.chatSidebar.loop_waiting_on_you'),
      glyph: a.goalLoop ? <Goal size={px} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden /> : <MonitorRadar actionRunning={false} className="text-warn" />,
    }
  }
  if (a.key === 'goal_loop' && a.goalLoopStalled) return { key: 'goal_loop_stalled', label: i18nT('pages.chatSidebar.goal_loop_interrupted_title'), glyph: <Goal size={px} className="shrink-0 text-danger" aria-hidden /> }
  if (a.key === 'interrupted') return { key: a.key, label: i18nT('pages.chat.recoveryCard.turn_interrupted'), glyph: <TriangleAlert size={px} className="shrink-0 text-danger" aria-hidden /> }
  if (a.key === 'structured_monitor_passive' && a.monitorBlocked) return { key: a.key, label: a.monitorLabel, glyph: <MonitorRadar actionRunning={false} className="text-warn" /> }
  if (a.key === 'crew_running') return { key: a.key, label: a.crewRunningText, glyph: <MonitorRadar actionRunning={false} className="text-accent" /> }
  if (a.subtreeNeedsYou) return { key: 'subtree_needs_you', label: i18nT('pages.chatSidebar.needs_your_answer'), glyph: <MessageCircleQuestionMark size={px} className="shrink-0 text-accent" aria-hidden /> }
  if (a.unread) return { key: 'unread', label: i18nT('pages.chatSidebar.agent_finished_your_turn'), glyph: <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--ok)' }} /> }
  return null
}

export function CompactMarkView({ mark }: { mark: CompactMark }) {
  return (
    <span className="inline-flex items-center align-middle mr-1.5" role="img" aria-label={mark.label} title={mark.label} data-testid="session-compact-attention" data-state={mark.key}>
      {mark.glyph}
    </span>
  )
}

/** A Crew Mode root's title-line marks: the live dot while a crew member works,
 *  the bolt (the hidden agent line names the mode), Paused, and ONE attention
 *  mark. The crew's ask is the count the full card's summary line says
 *  (`useCrewRootNeedsYou`), so a remote worker's approval or a ledger item on the
 *  person shows here too. It gives way only to a state the root itself owes
 *  (`ownOwed`), as on the full card; any other row mark gives way to it. A
 *  crew member works when a local row runs or the worker list (remote workers
 *  included) counts one working. A failed list read says so, with the same
 *  notice and hand-off as the summary line, instead of stale counts. */
export function CompactCrewMarks({ root, work, own, ownOwed, working, rowDot, dotCls, iconPx }: {
  root: string; work: CrewWorkFacts | null | undefined; own: CompactMark | null; ownOwed: boolean
  /** A local crew member's row runs. */
  working: boolean
  /** The row already lights its own live dot. */
  rowDot: boolean; dotCls: string; iconPx: number
}) {
  const { lastWorkers, paused, failed } = useCrewWorkerSummary(root)
  const needsYou = useCrewRootNeedsYou(root, work, lastWorkers)
  const listedRunning = crewRowCounts(summarizeCrewWorkers(lastWorkers ?? []).groups).find(c => c.bucket === 'working')?.count ?? 0
  const mark = own && ownOwed ? own : !failed && needsYou > 0
    ? { key: 'crew_needs_you', label: i18nT('components.crewMode.ultra_row_count_needs_you', { count: needsYou }), glyph: <Hand size={iconPx} className="shrink-0 crew-tone-warn" aria-hidden /> }
    : own
  const pausedLabel = i18nT('components.crewMode.ultra_paused')
  return (
    <>
      {!rowDot && !failed && (working || listedRunning > 0) && !paused && !mark && (
        <span className={dotCls} role="img" aria-label={i18nT('pages.chatSidebar.running_marker')} title={i18nT('pages.chatSidebar.running_marker')} data-testid="session-live-dot" />
      )}
      <span className="inline-flex items-center align-middle mr-1.5" data-testid="session-compact-crew" role="img" aria-label={i18nT('components.crewMode.eyebrow')} title={i18nT('components.crewMode.eyebrow')}>
        <Zap size={iconPx} className="shrink-0 ultra-accent" aria-hidden />
      </span>
      {paused && (
        <span className="inline-flex items-center align-middle mr-1.5 crew-tone-paused" data-testid="session-compact-crew-paused" role="img" aria-label={pausedLabel} title={pausedLabel}>
          <Pause size={iconPx} className="shrink-0" aria-hidden />
        </span>
      )}
      {mark && <CompactMarkView mark={mark} />}
      {failed && <ErrorNotice variant="inline" askAgent className="min-w-0 mr-1.5" messageClassName="truncate" message={i18nT('commandCenter.workers_error')} />}
    </>
  )
}

/** A title-only card's title line: its marks BESIDE the title element, never in
 *  it, because the row's double-click rename and the scroll peek both read
 *  `[data-session-title]`, which must hold only the title. Off (a full card, or
 *  a rename in progress), it renders the title element alone, unchanged. */
export function CompactTitleRow({ on, marks, children }: { on: boolean; marks: ReactNode; children: ReactNode }) {
  if (!on) return <>{children}</>
  return (
    <div className="flex items-center min-w-0 [&>[data-session-title]]:min-w-0 [&>[data-session-title]]:flex-1">
      <span className="inline-flex shrink-0 items-center" data-testid="session-compact-marks">{marks}</span>
      {children}
    </div>
  )
}
