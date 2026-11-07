/**
 * The live mini-transcript inside a Subagents-panel card: what the sub-agent
 * is doing, as it does it, in the chat's own shape. Reasoning rows fold the
 * way `ThinkingBlock` folds the parent's, each tool call is a row that opens on
 * its input and output, and the agent's answer text renders as markdown
 * between them. Rows come from `SubagentActivity.transcript`, which the
 * `subagent_thinking` / `subagent_tool` / `subagent_tool_result` frames and the
 * text chunks build in arrival order (the subagent reducers behind the
 * chatSlice facade).
 *
 * This replaces the card's single "Output" box for a card that has rows: that
 * box only ever showed the agent's streamed answer, and a sub-agent that works
 * through tools for most of its run streams none until the end -- so the card
 * said "Waiting for output…" under "Starting…" for the whole run.
 *
 * The reasoning row is its own small disclosure rather than `ThinkingBlock`.
 * That block is the parent transcript's: it sizes itself to the message
 * column (`ROW_PILL_*` geometry, a live one-line preview ticker, the smoothing
 * stream buffer and the Settings switch behind it, a disclosure key for the
 * virtualised list), and it reads the chat's own stream-idle timer. Inside a
 * 12px card list none of that applies -- rows are not virtualised, there is
 * no message column to align to, and the card's own row says what is live --
 * so the row keeps only the fold and the two labels, and shares the block's
 * i18n keys so the wording cannot drift.
 */
import { useEffect, useRef, useState } from 'react'
import { CheckCircle, ChevronRight, Loader as LoaderIcon, Sparkles, Wrench, XCircle } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { ToolInputText } from '../../components/ToolInputText'
import { Btn } from '../../components/ui'
import type { SubagentActivity, SubagentTranscriptItem } from '../../types'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { i18nT } from '../../i18n/t'

/** kiro-cli prefixes a native child's live tool title with its own status word;
 *  the row's spinner, icon and state already say that, and it is false on a
 *  finished call. */
/** kiro-cli's live verb on a shell call's title. The rows strip it (their
 *  state says it) and so does the finished card's "Last tool" line, which
 *  would otherwise read as still running. */
export const RUNNING_PREFIX = /^Running: /

/** The terminal statuses that mean a call FAILED. A result frame can carry a
 *  non-terminal status too (`pending`, `in_progress`: kiro-cli's content
 *  update for a call still running), which is not an outcome at all. */
const FAILED_TOOL_STATUSES = new Set(['failed', 'cancelled', 'canceled', 'refused'])

/** Slack, in px, for "the reader is at the end": an exact test never re-arms
 *  following, because sub-pixel offsets leave scrollTop a hair short. Same
 *  value as the card's output body. */
const TAIL_SLACK_PX = 20

type ToolRow = Extract<SubagentTranscriptItem, { kind: 'tool' }>

/** The first line of a reasoning text with anything on it, for the folded row's
 *  preview. Several folded rows read "Thought process" alike; the preview is
 *  what tells them apart without opening each. */
function firstLine(text: string): string {
  const line = sanitizeLlmOutput(text).split('\n').find(l => l.trim())
  return line ? line.trim() : ''
}

function ThinkingRow({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false)
  const preview = open ? '' : firstLine(text)
  return (
    <li className="text-[12px]" data-testid="subagent-transcript-thinking">
      <Btn
        type="button"
        className="!p-0 !border-none !rounded-none gap-1 max-w-full min-w-0 text-[12px] text-muted/60 hover:text-muted hover:bg-transparent italic"
        aria-expanded={open}
        aria-label={open ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.thinkingBlock.show_reasoning')}
        onClick={e => { e.stopPropagation(); setOpen(o => !o) }}
      >
        <ChevronRight className={`lucide-inline shrink-0 transition-transform${open ? ' rotate-90' : ''}`} aria-hidden="true" />
        <Sparkles className="lucide-inline shrink-0" aria-hidden="true" />
        <span className="shrink-0">{live ? i18nT('pages.chat.thinkingBlock.thinking') : i18nT('pages.chat.thinkingBlock.thought_process')}</span>
        {preview && (
          // The folded row's one-line preview, as the parent's ThinkingBlock
          // carries its live tail: content, not a label, so it is hidden from
          // the accessible name the button already has, and truncated at the
          // row's edge rather than wrapped.
          <span aria-hidden data-testid="subagent-transcript-thinking-preview" className="min-w-0 truncate not-italic text-muted/50 font-body">{preview}</span>
        )}
      </Btn>
      {open && (
        // Reasoning is dim, pre-wrapped text rather than markdown: a thought
        // stream is often partial and ill-formed, as in the parent's block.
        <pre className="mt-1 ml-4 text-[11px] text-muted/60 whitespace-pre-wrap break-words font-body max-h-[160px] overflow-y-auto m-0">{sanitizeLlmOutput(text)}</pre>
      )}
    </li>
  )
}

function ToolRowView({ row, live, isRunning, cardCompleted }: { row: ToolRow; live: boolean; isRunning: boolean; cardCompleted: boolean }) {
  const [open, setOpen] = useState(false)
  // A call first seen through its result has no title until the refinement
  // that follows names it; the row says what it is rather than standing blank.
  const label = row.tool ? sanitizeLlmOutput(row.tool.replace(RUNNING_PREFIX, '')) : i18nT('pages.chat.activityViewer.untitled_call')
  const failed = !!row.status && FAILED_TOOL_STATUSES.has(row.status)
  const completed = row.status === 'completed'
  // A call whose newest frame says it is still running (a content update
  // before the terminal one) is live while the agent is, wherever it sits.
  const running = live || (isRunning && !!row.status && !failed && !completed)
  // A failure is a detail in itself: a call that failed with no output still
  // opens, on the error surface, so the failure is never just a red glyph.
  const hasDetails = !!row.input || !!row.output || failed
  // A failed call on a card that is still running is usually an ordinary
  // non-zero exit the agent is already handling (a reviewer's failing test),
  // so its closed row is muted rather than red: red on every such row made a
  // working agent read as failing. Opened, it is still the error surface with
  // the hand-off. A card that COMPLETED keeps them muted -- the agent finished
  // with them handled, and "Complete" over red rows read as a contradiction --
  // while a card that ended in failure, or was stopped before it could finish
  // with them, shows them red. The glyph is a circled X in both states -- a
  // grey circled "!" read as a note, and then "turned into" a failure when the
  // card finished -- so the end of the run changes the emphasis, not what the
  // row says.
  const muted = failed && (isRunning || cardCompleted)
  const state = running
    ? <LoaderIcon className="lucide-inline animate-spin text-accent" aria-label={i18nT('pages.chat.activityViewer.running')} />
    : failed
      ? <XCircle className={`lucide-inline ${muted ? 'text-muted/60' : 'text-danger'}`} aria-label={i18nT('pages.chat.activityViewer.error')} />
      : completed
        ? <CheckCircle className="lucide-inline text-ok" aria-label={i18nT('pages.chat.activityViewer.complete')} />
        : <Wrench className="lucide-inline" aria-hidden="true" />
  return (
    <li className="text-[12px] font-mono" data-testid="subagent-transcript-tool" data-tool-status={row.status || (live ? 'running' : undefined)} aria-current={running ? 'step' : undefined}>
      <div className={`flex items-center gap-1 min-w-0 ${running ? 'text-accent' : failed && !muted ? 'text-danger/80' : 'text-muted/80'}`}>
        <span className="shrink-0 inline-flex">{state}</span>
        {hasDetails
          ? (
            // The call's title is the toggle's accessible name: an `aria-label`
            // would name every row alike, and `aria-expanded` already says open
            // or closed.
            <Btn
              type="button"
              className="!p-0 !border-none !rounded-none gap-1 min-w-0 text-[12px] font-mono text-inherit hover:text-text hover:bg-transparent text-left"
              aria-expanded={open}
              title={label}
              onClick={e => { e.stopPropagation(); setOpen(o => !o) }}
            >
              <ChevronRight className={`lucide-inline shrink-0 transition-transform${open ? ' rotate-90' : ''}`} aria-hidden="true" />
              <span className="truncate">{label}</span>
            </Btn>
          )
          : <span className="truncate" title={label}>{label}</span>}
      </div>
      {open && hasDetails && (
        <div className="ml-4 mt-1 space-y-1.5">
          {row.input && (
            <div>
              <div className="text-[10px] text-muted/40 uppercase tracking-wider font-body">{i18nT('pages.chat.activityViewer.input')}</div>
              <pre className="text-[11px] text-muted/80 whitespace-pre-wrap break-words max-h-[160px] overflow-y-auto m-0"><ToolInputText text={row.input} /></pre>
            </div>
          )}
          {failed
            ? (
              // A failed call's output is its diagnostics -- the outcome of
              // something that failed -- so it renders as the error surface,
              // with the hand-off on: the card is a status panel holding no
              // draft, and the agent is usually the one who can act on it.
              // The raw output stays the message (verbatim, mono, scrolling
              // inside the bound); the title carries the meaning. A failure
              // reported by status alone (kiro-cli's terminal frame can carry
              // no output) still renders the surface, with the failure named
              // in place of the output it does not have.
              <ErrorNotice
                className="font-body max-h-[240px]"
                title={i18nT('pages.chat.activityViewer.tool_call_failed')}
                message={row.output ? sanitizeLlmOutput(row.output) : i18nT('pages.chat.activityViewer.tool_call_failed_no_output')}
                messagePlacement="below"
                messageClassName={row.output ? 'font-mono' : undefined}
                scrollMessage
                actionPlacement="below"
                askAgent
                askAgentLabel={i18nT('pages.chat.activityViewer.ask_agent_about_failed_call')}
                testId="subagent-transcript-tool-error"
              />
            )
            : row.output && (
              <div>
                <div className="text-[10px] text-muted/40 uppercase tracking-wider font-body">{i18nT('pages.chat.activityViewer.output')}</div>
                <pre className="text-[11px] text-muted/80 whitespace-pre-wrap break-words max-h-[240px] overflow-y-auto m-0">{sanitizeLlmOutput(row.output)}</pre>
              </div>
            )}
        </div>
      )}
    </li>
  )
}

/** Which edges of the scroller have rows beyond them. */
function edgesBeyond(el: HTMLElement) {
  return { top: el.scrollTop > 0, bottom: el.scrollHeight - el.scrollTop - el.clientHeight > 1 }
}

export default function SubagentTranscript({ a, isRunning, isDone }: { a: SubagentActivity; isRunning: boolean; isDone: boolean }) {
  const rows = a.transcript ?? []
  const listRef = useRef<HTMLOListElement>(null)
  // Follow the newest row the way the output body does: stay pinned to the
  // bottom unless the reader scrolled up to read an earlier one.
  const follow = useRef(true)
  // Rows beyond an edge of the box are cut mid-line; fade that edge so the cut
  // reads as "more above" / "more below" rather than a rendering fault.
  const [fade, setFade] = useState({ top: false, bottom: false })
  const last = rows[rows.length - 1]
  const lastLen = last ? (last.kind === 'tool' ? (last.output?.length ?? 0) : last.text.length) : 0
  // Keyed by the newest row's `seq`, not the row count: past the cap each new
  // row drops the oldest, so the count stays put while the list keeps growing.
  useEffect(() => {
    const el = listRef.current
    if (!el) return
    if (follow.current) el.scrollTop = el.scrollHeight
    setFade(edgesBeyond(el))
  }, [last?.seq, lastLen])
  if (!rows.length) return null
  const omitted = a.transcriptOmitted ?? 0
  // Above the coalescing threshold the backend merges tool frames (latest
  // wins), so calls the card never heard of are counted by `toolCount` alone;
  // say how many rather than implying the rows are the whole run.
  const toolRows = rows.reduce((n, r) => n + (r.kind === 'tool' ? 1 : 0), 0)
  const unlistedCalls = Math.max(0, (a.toolCount ?? 0) - toolRows - omitted)
  // A 24px fade on each edge rows sit beyond, so a row cut by the box's edge
  // reads as "more this way" rather than a rendering fault.
  const mask = fade.top && fade.bottom
    ? 'linear-gradient(to bottom,transparent,#000 24px,#000 calc(100% - 24px),transparent)'
    : fade.top ? 'linear-gradient(to bottom,transparent,#000 24px)'
      : fade.bottom ? 'linear-gradient(to top,transparent,#000 24px)'
        : undefined
  return (
    <div className="px-3 pb-2" data-testid="subagent-transcript">
      {/* "Live activity" while the card can still grow; a finished card's rows
          are what the agent did, so the heading drops the "live". */}
      <div className="text-[10px] text-muted/40 uppercase tracking-wider mb-1" data-testid="subagent-transcript-heading">
        {i18nT(isDone ? 'pages.chat.activityViewer.activity' : 'pages.chat.activityViewer.live_activity')}
        {omitted > 0
          ? <span className="normal-case tracking-normal text-muted/70" data-testid="subagent-transcript-omitted"> · {i18nT('pages.chat.activityViewer.transcript_steps_not_listed', { count: omitted })}</span>
          : unlistedCalls > 0 && <span className="normal-case tracking-normal text-muted/70" data-testid="subagent-transcript-unlisted"> · {i18nT('pages.chat.activityViewer.tool_calls_not_listed', { count: unlistedCalls })}</span>}
      </div>
      {/* The solid box stays put; only the inner scroller is masked, so the
          fades dim rows sliding out of view rather than the box's own edges. */}
      <div className="px-2.5 py-2 bg-bg rounded-md">
      <ol
        ref={listRef}
        onScroll={e => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < TAIL_SLACK_PX; setFade(edgesBeyond(el)) }}
        data-fade-top={fade.top || undefined}
        data-fade-bottom={fade.bottom || undefined}
        style={mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined}
        className="max-h-[320px] overflow-y-auto space-y-1 list-none m-0 p-0 leading-relaxed"
      >
        {rows.map((row, i) => {
          // Only the newest row can still be live: an earlier tool row is
          // finished whether or not its result frame named a status.
          const live = isRunning && i === rows.length - 1
          // Keyed by `seq`, not position: past the cap every new row shifts the
          // positions down by one, and a position key would carry an expanded
          // row's open state onto the row that slid into its place.
          if (row.kind === 'thinking') return <ThinkingRow key={row.seq} text={row.text} live={live} />
          if (row.kind === 'tool') return <ToolRowView key={row.seq} row={row} live={live && !row.status} isRunning={isRunning} cardCompleted={a.status === 'done'} />
          return (
            <li key={row.seq} className="text-[12px] text-muted/80 break-words" data-testid="subagent-transcript-text">
              <MarkdownRenderer content={row.text} streaming={live} softBreaks readOnlyCode />
            </li>
          )
        })}
      </ol>
      </div>
    </div>
  )
}
