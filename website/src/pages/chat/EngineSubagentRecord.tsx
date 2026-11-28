import { memo, useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { Clock, Coins } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import { fmtCredits, fmtElapsed, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { useAppDispatch, useAppSelector } from '../../store'
import { isActiveSubagent, openActivityToTab, selectSubagent } from '../../store/chatSlice'
import type { SubagentActivity } from '../../types'
import { CARD_CHIP, CompletionCardFrame } from './SubagentCompletionCard'
import type { ParsedBatchCompletion, ParsedSubagentCompletion } from '../../lib/subagentCompletion'

/** How one engine sub-agent ended, as the engine reported it, or `not_reported` when
 *  the turn ended before the engine said. */
export type EngineChildEnd = 'completed' | 'failed' | 'not_reported'

export interface EngineChildEntry {
  agent: string
  task: string
  end: EngineChildEnd
  reason?: string
  tools: string[]
  toolCount: number
  /** When the card opened and closed, ms, on the gateway's clock. Absent when the
   *  record holds no time (one written before it kept any). */
  startedAt?: number
  endedAt?: number
}

export interface EngineSubagentRecordData {
  children: EngineChildEntry[]
  more: number
  /** The whole turn's credits, sub-agents included. The engine reports no share per
   *  child, so no child carries one. */
  turnCredits?: number
  /** The gateway's key for the turn, which its live card carried. */
  turn?: string
  /** How every child ended, the unlisted ones too. Absent in a record written before
   *  the gateway kept it. */
  ends?: Record<EngineChildEnd, number>
  /** The whole batch's first start and last end, ms, the unlisted children too. */
  startedAt?: number
  endedAt?: number
}

/** The longest task a row draws; the gateway keeps at most as much. */
const TASK_SHOWN = 600
const ENDS: readonly EngineChildEnd[] = ['completed', 'failed', 'not_reported']

const str = (v: unknown, max: number): string => (typeof v === 'string' ? v.slice(0, max) : '')
const count = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0)
const positive = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : undefined)

/**
 * The reply row's `meta.engine_subagents`, validated: the KAS engine sub-agents the
 * turn ran (`dashboard/engine_subagent_record.py`). `null` for any other shape, so a
 * reply without one, or with a malformed one, draws exactly what it drew.
 */
export function engineRecordOf(raw: unknown): EngineSubagentRecordData | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const rec = raw as Record<string, unknown>
  if (!Array.isArray(rec.children)) return null
  const children: EngineChildEntry[] = []
  for (const item of rec.children) {
    if (!item || typeof item !== 'object') continue
    const e = item as Record<string, unknown>
    const end = ENDS.includes(e.end as EngineChildEnd) ? (e.end as EngineChildEnd) : 'not_reported'
    const tools = Array.isArray(e.tools) ? e.tools.filter((t): t is string => typeof t === 'string').map(t => t.slice(0, 80)) : []
    const started = positive(e.started_at)
    const ended = positive(e.ended_at)
    children.push({
      agent: str(e.agent, 80),
      task: str(e.task, TASK_SHOWN),
      end,
      ...(end === 'failed' && typeof e.reason === 'string' && e.reason ? { reason: e.reason.slice(0, 400) } : {}),
      tools,
      toolCount: Math.max(count(e.tool_count), tools.length),
      ...(started !== undefined && ended !== undefined && ended >= started ? { startedAt: started * 1000, endedAt: ended * 1000 } : {}),
    })
  }
  if (children.length === 0) return null
  const credits = positive(rec.turn_credits)
  const turn = typeof rec.turn === 'string' && /^[0-9a-f]{1,32}$/.test(rec.turn) ? rec.turn : undefined
  const rawEnds = rec.ends && typeof rec.ends === 'object' && !Array.isArray(rec.ends) ? rec.ends as Record<string, unknown> : null
  const ends = rawEnds ? { completed: count(rawEnds.completed), failed: count(rawEnds.failed), not_reported: count(rawEnds.not_reported) } : undefined
  const first = positive(rec.started_at)
  const last = positive(rec.ended_at)
  return {
    children,
    more: count(rec.more),
    ...(credits !== undefined ? { turnCredits: credits } : {}),
    ...(turn ? { turn } : {}),
    ...(ends && ends.completed + ends.failed + ends.not_reported >= children.length ? { ends } : {}),
    ...(first !== undefined && last !== undefined && last >= first ? { startedAt: first * 1000, endedAt: last * 1000 } : {}),
  }
}

/** A row's state: how the engine said it ended, or `running` on the live card. */
type RowEnd = EngineChildEnd | 'running'

interface ChildView extends Omit<EngineChildEntry, 'end'> {
  end: RowEnd
  /** How long it ran, ms; absent when unknown. */
  ranMs?: number
}

const END_KEY: Record<RowEnd, string> = {
  completed: 'pages.chat.engineSubagentRecord.end_completed',
  failed: 'pages.chat.engineSubagentRecord.end_failed',
  not_reported: 'pages.chat.engineSubagentRecord.end_not_reported',
  running: 'pages.chat.subagentRunCard.state_running',
}

function ChildRow({ child }: { child: ChildView }) {
  const hidden = child.toolCount - child.tools.length
  return (
    <li className="flex flex-col gap-0.5 min-w-0 py-1 border-t border-border first:border-t-0" data-testid="engine-record-child" data-end={child.end}>
      <div className="flex items-baseline gap-1.5 min-w-0">
        <span className={`shrink-0 font-medium ${child.end === 'failed' ? 'text-danger' : 'text-text'}`} data-testid="engine-record-end">
          {i18nT(END_KEY[child.end])}
        </span>
        {child.agent && <span className="min-w-0 truncate font-mono text-[11px]" translate="no">{child.agent}</span>}
        {child.ranMs !== undefined && (
          <span className="shrink-0 ml-auto tabular-nums opacity-75" data-testid="engine-record-ran" title={i18nT('pages.chat.engineSubagentRecord.ran_title')}>
            {fmtElapsed(child.ranMs, { wholeSeconds: child.end === 'running' })}
          </span>
        )}
      </div>
      {/* Model-written text: rendered as text, never as markup, clamped. */}
      {child.task && <p className="m-0 whitespace-pre-wrap break-words line-clamp-3 text-text" data-testid="engine-record-task">{child.task}</p>}
      {/* Why the engine says the child failed: backend failure text, so it takes the
          shared notice. No hand-off: this row sits under a reply in every transcript
          host, ChatEmbed among them, whose composer draft lives only in
          useComposerDraft state; navigating away would discard that unsent text. */}
      <ErrorNotice variant="inline" message={child.reason} testId="engine-record-reason" />
      {child.tools.length > 0 && (
        <div className="flex flex-col gap-0.5 min-w-0">
          <span className="opacity-75">{i18nT('pages.chat.activityViewer.tool_calls')}</span>
          {hidden > 0 && <span className="opacity-75">{i18nT('pages.chat.activityViewer.tool_calls_not_listed', { count: hidden })}</span>}
          <ul className="m-0 p-0 list-none flex flex-col gap-0.5 min-w-0">
            {child.tools.map((title, i) => (
              <li key={`${i}-${title}`} className="min-w-0 truncate font-mono text-[11px]" title={title}>{title}</li>
            ))}
          </ul>
        </div>
      )}
    </li>
  )
}

function ChildList({ rows, more }: { rows: ChildView[]; more: number }) {
  return (
    <div className="text-[12px] leading-5 text-muted min-w-0">
      <ul className="m-0 p-0 list-none min-w-0">
        {rows.map((child, i) => <ChildRow key={i} child={child} />)}
      </ul>
      {more > 0 && (
        <p className="m-0 pt-1 opacity-75">{i18nT('pages.chat.engineSubagentRecord.more_children', { n: fmtNumber(more) })}</p>
      )}
    </div>
  )
}

/** How long the batch ran: from its first child's start to its last one's end, ms. */
function DurationChip({ ms, live }: { ms: number; live: boolean }) {
  const duration = fmtElapsed(ms, { wholeSeconds: live })
  return (
    <span
      className={`${CARD_CHIP} bg-bg-hover border-border text-muted tabular-nums`}
      title={i18nT('pages.chat.engineSubagentRecord.duration_title')}
      data-testid="engine-batch-duration"
    >
      <Clock size={10} aria-hidden /> <span aria-hidden="true">{duration}</span>
      <span className="sr-only">{i18nT('pages.chat.engineSubagentRecord.took', { duration })}</span>
    </span>
  )
}

/** The turn's credits. The engine reports none per sub-agent, so this is the only
 *  credit figure the card shows, and it says it is the whole turn's. */
function TurnCreditsChip({ credits }: { credits: number }) {
  return (
    <span
      className={`${CARD_CHIP} bg-bg-hover border-border text-muted`}
      title={i18nT('pages.chat.engineSubagentRecord.turn_credits_title')}
      data-testid="engine-batch-turn-credits"
    >
      <Coins size={10} aria-hidden /> {i18nT('pages.chat.engineSubagentRecord.turn_credits', { credits: fmtCredits(credits) })}
    </span>
  )
}

/** The batch as the shared card reads it. The wave is "finished" only when the engine
 *  reported how every child ended; otherwise the card says how many it reported. */
function batchOf(ok: number, failed: number, stopped: number, total: number, running: number): ParsedBatchCompletion {
  const delivered = ok + failed + stopped
  return { kind: 'batch', chunk: 1, chunks: 1, final: delivered === total, ok, failed, stopped, total, delivered, running, body: '' }
}

/** First start to last end over the rows that have both, ms; null when none has. */
function spanOf(rows: { startedAt?: number; endedAt?: number }[]): number | null {
  let first = Infinity
  let last = -Infinity
  for (const r of rows) {
    if (r.startedAt === undefined || r.endedAt === undefined) continue
    first = Math.min(first, r.startedAt)
    last = Math.max(last, r.endedAt)
  }
  return Number.isFinite(first) && last >= first ? last - first : null
}

/**
 * The transcript's lasting record of the KAS engine sub-agents one reply's turn ran,
 * drawn as the same card a `spawn_run` wave's completion is: how many finished, the
 * success and failure counts, how long the batch ran, the turn's credits, Panel and
 * Show details over each child's agent, task, end and tool-call titles. Times are
 * Kiro Crew's own clock; the engine reports no credits or model for one child, so the
 * only credit figure is the whole turn's.
 */
const EngineSubagentRecord = memo(function EngineSubagentRecord({
  record,
  disclosureKey,
  onOpenPanel,
}: {
  record: EngineSubagentRecordData
  disclosureKey?: string
  /** Opens the Subagents side panel. Absent (a host with no side panel): no Panel control. */
  onOpenPanel?: (parsed: ParsedSubagentCompletion) => void
}) {
  useLanguageGeneration()
  // The whole batch's counts when the record kept them; a record without them counts
  // the children it lists, and says only how many ends it reported.
  const parsed = useMemo(() => {
    const e = record.ends
    if (e) return batchOf(e.completed, e.failed, 0, e.completed + e.failed + e.not_reported, 0)
    const ok = record.children.filter(c => c.end === 'completed').length
    const failed = record.children.filter(c => c.end === 'failed').length
    return batchOf(ok, failed, 0, record.children.length + record.more, 0)
  }, [record])
  const rows = useMemo<ChildView[]>(() => record.children.map(c => ({
    ...c,
    ...(c.startedAt !== undefined && c.endedAt !== undefined ? { ranMs: c.endedAt - c.startedAt } : {}),
  })), [record])
  const span = spanOf(record.startedAt !== undefined ? [record] : record.children)
  // The same layout id the turn's live card had, so the card moves from the foot of
  // the chat to under the reply as one element (framer-motion; reduced motion skips it).
  return (
    <motion.div className="w-full min-w-0 mt-1" layoutId={record.turn ? `engine-batch-${record.turn}` : undefined}>
      <CompletionCardFrame
        parsed={parsed}
        testId="engine-subagent-record"
        detailsTestId="engine-subagent-record-toggle"
        disclosureKey={disclosureKey}
        onOpenPanel={onOpenPanel ? () => onOpenPanel(parsed) : undefined}
        extraChips={span !== null || record.turnCredits !== undefined ? <>
          {span !== null && <DurationChip ms={span} live={false} />}
          {record.turnCredits !== undefined && <TurnCreditsChip credits={record.turnCredits} />}
        </> : undefined}
        body={<ChildList rows={rows} more={record.more} />}
      />
    </motion.div>
  )
})

export default EngineSubagentRecord

const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}
function liveEnd(a: SubagentActivity): RowEnd {
  if (isActiveSubagent(a)) return 'running'
  if (a.status === 'done') return 'completed'
  if (a.status === 'error') return 'failed'
  return 'not_reported'
}

/**
 * The live form of the record, at the foot of the transcript while the chat's turn
 * runs KAS engine sub-agents: the same card, with a spinning glyph and a ticking
 * duration while any child runs. Draws nothing when the turn runs none, which is
 * every turn with the `kas_subagents` preview off: only an engine child's card is
 * marked `engine`. After the turn the reply's own record takes over.
 */
export const EngineSubagentLiveCard = memo(function EngineSubagentLiveCard({ slot, running, turnMark = '' }: {
  slot: string | null
  running: boolean
  /** Names the turn the chat is on: the slot's own `turn_gen:turn` identity. A queued
   *  successor starts with no pause in `running`, and its new mark ends the earlier batch. */
  turnMark?: string
}) {
  useLanguageGeneration()
  const dispatch = useAppDispatch()
  const subagents = useAppSelector(s => (slot && slot === s.chat.activeSlot ? s.chat.subagents : s.chat.slotActivity[slot ?? '']?.subagents ?? EMPTY_SUBAGENTS))
  const engine = useMemo(() => Object.values(subagents).filter(a => a.engine), [subagents])
  // The turn this card draws: the gateway's key (`engineTurn`) on an engine card still
  // open, which can only be this turn's, since a turn's end closes every one. Kept
  // once seen, so this turn's cards stay listed after they end; an earlier turn's
  // cards carry another key.
  // shortcut: a page opened after every sub-agent of a still-running turn ended sees
  // no open card and draws nothing until the reply's record lands at the turn's end.
  // The newest open card: one whose done frame this page missed can stay open in the store.
  const openKey = engine
    .filter(a => isActiveSubagent(a) && a.engineTurn)
    .reduce<SubagentActivity | undefined>((newest, a) => (!newest || a.startedAt > newest.startedAt ? a : newest), undefined)?.engineTurn
  const [seen, setSeen] = useState<{ slot: string | null; mark: string; key: string } | null>(null)
  useEffect(() => {
    if (!running) setSeen(null)
    else if (openKey) setSeen({ slot, mark: turnMark, key: openKey })
  }, [running, openKey, slot, turnMark])
  const kept = seen && seen.slot === slot && seen.mark === turnMark ? seen.key : undefined
  const key = running ? openKey ?? kept : undefined
  const members = useMemo(() => (key ? engine.filter(a => a.engineTurn === key) : []), [engine, key])
  const active = members.filter(isActiveSubagent).length
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (active === 0) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [active])
  if (!running || members.length === 0) return null

  const rows: ChildView[] = members.map(a => {
    const end = liveEnd(a)
    const ranMs = end === 'running' ? now - a.startedAt : a.elapsed * 1000
    return {
      agent: a.agent.slice(0, 80),
      task: a.task.slice(0, TASK_SHOWN),
      end,
      ...(end === 'failed' && a.error ? { reason: a.error.slice(0, 400) } : {}),
      tools: (a.toolCalls ?? []).map(t => t.tool.slice(0, 80)),
      toolCount: Math.max(a.toolCount ?? 0, a.toolCalls?.length ?? 0),
      ...(Number.isFinite(ranMs) && ranMs >= 0 ? { ranMs } : {}),
    }
  })
  const parsed = batchOf(
    rows.filter(r => r.end === 'completed').length,
    rows.filter(r => r.end === 'failed').length,
    rows.filter(r => r.end === 'not_reported').length,
    rows.length,
    active,
  )
  const first = Math.min(...members.map(a => a.startedAt))
  const last = active > 0 ? now : Math.max(...members.map(a => a.startedAt + a.elapsed * 1000))
  const open = () => {
    dispatch(selectSubagent(members[0].id))
    dispatch(openActivityToTab('subagents'))
  }
  // Row geometry: this card sits in the transcript's foot, outside the row wrapper
  // every message gets, so it takes the same gutter and width clamp itself.
  return (
    <div className="px-4 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <motion.div layoutId={`engine-batch-${key}`}>
      <CompletionCardFrame
        parsed={parsed}
        running={active}
        testId="engine-subagent-live"
        detailsTestId="engine-subagent-live-toggle"
        onOpenPanel={open}
        extraChips={last >= first ? <DurationChip ms={last - first} live={active > 0} /> : null}
        body={<ChildList rows={rows} more={0} />}
      />
      </motion.div>
    </div>
  )
})
