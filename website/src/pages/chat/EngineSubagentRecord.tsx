import { memo, useId } from 'react'
import { Bot, ChevronRight } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { useRowDisclosure } from './rowDisclosure'

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
}

export interface EngineSubagentRecordData {
  children: EngineChildEntry[]
  more: number
}

/** The longest task a row draws; the gateway keeps at most as much. */
const TASK_SHOWN = 600
const ENDS: readonly EngineChildEnd[] = ['completed', 'failed', 'not_reported']

const str = (v: unknown, max: number): string => (typeof v === 'string' ? v.slice(0, max) : '')
const count = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.floor(v) : 0)

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
    children.push({
      agent: str(e.agent, 80),
      task: str(e.task, TASK_SHOWN),
      end,
      ...(end === 'failed' && typeof e.reason === 'string' && e.reason ? { reason: e.reason.slice(0, 400) } : {}),
      tools,
      toolCount: Math.max(count(e.tool_count), tools.length),
    })
  }
  if (children.length === 0) return null
  return { children, more: count(rec.more) }
}

const END_KEY: Record<EngineChildEnd, string> = {
  completed: 'pages.chat.engineSubagentRecord.end_completed',
  failed: 'pages.chat.engineSubagentRecord.end_failed',
  not_reported: 'pages.chat.engineSubagentRecord.end_not_reported',
}

function ChildRow({ child }: { child: EngineChildEntry }) {
  const hidden = child.toolCount - child.tools.length
  return (
    <li className="flex flex-col gap-0.5 min-w-0 py-1 border-t border-border first:border-t-0" data-testid="engine-record-child" data-end={child.end}>
      <div className="flex items-baseline gap-1.5 min-w-0">
        <span className={`shrink-0 font-medium ${child.end === 'failed' ? 'text-danger' : 'text-text'}`} data-testid="engine-record-end">
          {i18nT(END_KEY[child.end])}
        </span>
        {child.agent && <span className="min-w-0 truncate font-mono text-[11px]" translate="no">{child.agent}</span>}
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

/**
 * The transcript's lasting record of the KAS engine sub-agents one reply's turn ran:
 * each child's agent, task, how it ended and its tool-call titles, exactly as the
 * engine reported them. Closed by default; the line names how many ran. It holds no
 * time, credits or model, because the engine reports none per child.
 */
const EngineSubagentRecord = memo(function EngineSubagentRecord({
  record,
  disclosureKey,
}: {
  record: EngineSubagentRecordData
  disclosureKey?: string
}) {
  useLanguageGeneration()
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const panelId = useId()
  const total = record.children.length + record.more
  return (
    <div className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted mt-1" data-testid="engine-subagent-record" data-expanded={expanded}>
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        aria-controls={panelId}
        className="w-full flex items-center gap-1.5 px-2 py-1 min-w-0 text-left text-[12px] leading-5 hover:text-text motion-safe:transition-colors"
        data-testid="engine-subagent-record-toggle"
      >
        <ChevronRight className={`lucide-inline shrink-0 motion-safe:transition-transform ${expanded ? 'rotate-90' : ''}`} aria-hidden="true" />
        <Bot className="lucide-inline shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate font-medium text-text">
          {i18nT('pages.chat.engineSubagentRecord.title', { n: fmtNumber(total) })}
        </span>
      </button>
      {expanded && (
        <div id={panelId} className="px-2 pb-1.5 text-[12px] leading-5 min-w-0">
          <ul className="m-0 p-0 list-none min-w-0">
            {record.children.map((child, i) => <ChildRow key={i} child={child} />)}
          </ul>
          {record.more > 0 && (
            <p className="m-0 pt-1 opacity-75">{i18nT('pages.chat.engineSubagentRecord.more_children', { n: fmtNumber(record.more) })}</p>
          )}
        </div>
      )}
    </div>
  )
})

export default EngineSubagentRecord
