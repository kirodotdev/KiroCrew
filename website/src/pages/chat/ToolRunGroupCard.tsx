/**
 * ToolRunGroupCard — one collapsible row for a contiguous RUN of tool calls.
 *
 * ## The grouping this implements (DFL-71 phase 1)
 *
 * TurnBlock's DEFAULT mode already folds a turn's tool calls behind one
 * "N tool calls" toggle; this component is the per-RUN layer inside that fold
 * shape. A `run` is a contiguous sequence of TWO OR MORE ordinary tool-role
 * singles in a turn — the contiguous run concept TurnBlock's splitSegments
 * already derives — collapsed into ONE header row whose label is the run's
 * DOMINANT verb (`Explored · 9 tool calls`). The reader keeps the shape of the
 * work at a glance, and the per-call rows are one click away in the expanded
 * view. A run of ONE keeps the plain row: wrapping a single pill in a second
 * disclosure buys nothing and doubles the click depth on the most common
 * shape (tool → prose → tool), and it keeps the disclosurePin /
 * disclosureDurable suites' single-button turns single-buttoned.
 *
 * The exemptions are NOT re-derived here. TurnBlock's splitSegments already
 * routes every non-foldable item (workflow_run / spawn_run cards, completion
 * cards, MCP-App rows, diff cards, errors, OAuth / app injects, widgets and
 * images, [OPTIONS:] hand-backs, keep-visible markers, legacy crew replies)
 * OUTSIDE any collapsed run; a run group therefore only ever receives the
 * ordinary tool rows TurnBlock's `isTool` accepted, and an exempted row between
 * two runs splits them instead of being swallowed. Approvals / permission rows
 * are likewise never passed here (groupDisplayItems routes them to the pinned
 * ApprovalBar / CollapsibleToolGroup); this component does not change that.
 *
 * ## Duration (phase 2 seam)
 *
 * ToolActivity carries no duration today, so the header shows verb + count
 * only. `summary.duration` is a deliberate slot: when the transport stamps a
 * duration on the log entries, TurnBlock can pass the run's summed duration
 * and the header renders `· 3m 12s` with no other change — `summarizeToolRun`
 * keeps the field carrying `null` until then.
 */

import { memo, useMemo } from 'react'
import { useRowDisclosure } from './rowDisclosure'
import ToolCallLine from './ToolCallLine'
import type { ToolRunSummary } from './toolRunSummary'
import { ChevronRight, Wrench } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

export interface ToolRunGroupProps {
  /** The messages of the contiguous run, in transcript order. */
  messages: ChatMessage[]
  /** The run's summary (verb, count, per-call labels). One computation for
   *  the group, shared by the header and the expanded rows. */
  summary: ToolRunSummary
  /** Whether the turn this run belongs to is still running. */
  turnRunning?: boolean
  /** Stable per-slot identity the disclosure state is held under (see
   *  rowDisclosure) — a run's key survives the virtualizer's recycling. */
  disclosureKey?: string
  /** Passed through to every per-call row. */
  slot?: string
  onFileOpen?: (path: string) => void
  transcriptHot?: boolean
}

/**
 * The run row, folded by default.
 *
 * Header shape — `Explored repo · 9 tool calls · 3m 12s`:
 *   - the verb is the dominant action across the run (see summarizeToolRun);
 *   - the count is DISTINCT calls (the 🔧 request rows — the same count the
 *     turn-level toggle reports; completion rows are never in `messages`);
 *   - the duration renders only when `summary.duration` is non-null, which
 *     today is never (ToolActivity has no duration). The seam is kept so
 *     phase 2 lights it up without touching this render.
 */
export default memo(function ToolRunGroup({
  messages, summary, turnRunning, disclosureKey, slot, onFileOpen, transcriptHot,
}: ToolRunGroupProps) {
  // memo() bails out of the provider-level repaint; subscribe directly (see
  // useLanguageGeneration's contract — once per memo() body, and the
  // generation rides the label memos' dependency lists below).
  useLanguageGeneration()
  // Folded by default — the whole point of the run group. Held in the durable
  // row-disclosure store under `disclosureKey` so the user's choice survives
  // the virtualizer unmounting the row mid-scroll.
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const countLabel = useMemo(
    () => i18nT('pages.chat.toolRunGroup.tool_calls', { count: summary.count }),
    [summary.count],
  )
  const durationLabel = useMemo(
    () => (summary.duration === null ? null : i18nT('pages.chat.toolRunGroup.duration', { duration: summary.duration })),
    [summary.duration],
  )

  return (
    <div className="my-1">
      {/* No `font-mono`: the header's verb is prose derived from the call's own
          arguments, exactly like the tool pill's label. The count sits in the
          muted tone so the verb stays the thing the eye anchors on. The visible
          label IS the accessible name (same rule as the turn toggle and the
          tool pill); aria-expanded carries the state the chevron alone never
          announces. */}
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="inline-flex items-start gap-2 min-w-0 max-w-full text-[13px] leading-5 px-2 py-0.5 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none transition-all text-left focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-hidden"
        aria-expanded={expanded}
      >
        {/* Lucide, never a text glyph: AUTOSDE use-lucide-icons and
            no-emoji-as-icons are blocking on src TSX. */}
        <Wrench size={12} aria-hidden="true" className="shrink-0 text-muted" style={{ marginTop: '4px' }} />
        <span className="truncate">
          {summary.title}
          <span className="text-muted/60"> · {countLabel}</span>
          {durationLabel && <span className="text-muted/60"> · {durationLabel}</span>}
        </span>
        <ChevronRight size={13} aria-hidden="true" className={`shrink-0 transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} style={{ marginTop: '4px' }} />
      </button>
      {/* The turn is still running → the expanded view is the record of what
          has happened SO FAR, not a settled history; leaving the rows mounted
          for a running turn keeps the newest pill's shimmer live. The per-call
          rows reuse ToolCallLine untouched: each derives its own short label
          and mounts ToolDetails on expand, so no command is ever repeated in
          the header AND the body — the header names the run's DOMINANT verb,
          never any single call's command. */}
      {expanded && (
        <div className="mt-1 ml-4 pl-3 shadow-[inset_2px_0_0_0_var(--border)] forced-colors:border-l-2 flex flex-col gap-1">
          {messages.map((m, i) => (
            <ToolCallLine
              key={typeof m.meta?.tool_call_id === 'string' && m.meta.tool_call_id ? m.meta.tool_call_id : `run-${summary.count}-${i}`}
              message={m}
              running={turnRunning === true}
              slot={slot}
              onFileOpen={onFileOpen}
              transcriptHot={transcriptHot}
            />
          ))}
        </div>
      )}
    </div>
  )
})
