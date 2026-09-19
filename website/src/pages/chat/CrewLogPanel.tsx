/**
 * The CREW LOG section of the chat right panel — the SPA consumer of the five
 * session folds `GET /api/sessions/{id}/crew-log/projection/{name}` serves.
 *
 * The backend folds; this renders (RFC NFR-2). Every number shown here is read
 * from a projection's `value`, and nothing is recomputed from entries: the panel
 * never reads the crew log itself, so it cannot disagree with the fold about the
 * same file.
 *
 * ONE query holds all five folds, because the panel presents them as one
 * section and a per-fold query would give the section five loading states and
 * five error states for a single refresh. Reads are scoped to the session the
 * tab belongs to — like the Logs and Context tabs, there is no session picker.
 *
 * REFRESH is edge-triggered, not polled. A crew log only grows while the session
 * is working, so a timer would re-read an unchanged file on every tick of an idle
 * session. Two falling edges trigger it, because two things append here and no one
 * signal sees both: the session's own turn stopping (`selectSlotStreamState`), and
 * all of its spawned work draining (`selectComposerBusy`, which stays true while
 * subagents run and is when a `subagent/spawned` entry is closed). A manual control
 * is offered beside the folded-through seq for the case the reader wants a value
 * mid-turn.
 *
 * A fold's `seq` is its version: the crew-log seq it was folded through. The
 * footer reports the highest of the five, which is what makes "this value is
 * older than the log" observable instead of implied.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { useAppSelector } from '../../store'
import { selectComposerBusy, selectSlotStreamState } from '../../store/chatSlice'
import { i18nT } from '../../i18n/t'
import { fmtCompact, fmtElapsed, fmtNumber, fmtPercent, fmtTimeNumeric } from '../../i18n/format'

/** The five folds, in the order the backend declares them (`PROJECTION_NAMES`). */
export const CREW_LOG_FOLDS = ['status', 'usage', 'timeline', 'tools', 'approvals'] as const
export type CrewLogFold = (typeof CREW_LOG_FOLDS)[number]

/** One fold's value at the seq it was folded through. */
export interface CrewLogProjection {
  session_id?: string
  name?: string
  seq: number
  value: Record<string, unknown>
}
export type CrewLogBundle = Record<CrewLogFold, CrewLogProjection>
/** What one read of the batch route answers: the five folds, plus the two things
 *  the folds themselves cannot say -- whether a unit was addressable for the id
 *  sent, and whether the writer owed entries as the fold was taken. */
export type CrewLogRead = {
  folds: CrewLogBundle
  resolved: boolean
  writesDrained: boolean
}

/** Rows a table renders before it says how many names it left out. A narrow
 *  panel column, so the cut is well below the backend's own per-name cap. */
const TABLE_ROWS = 12
/** Moments the timeline draws. The fold keeps a 200-moment window; drawing all
 *  of it inside a side panel is DOM nobody scrolls to. */
const TIMELINE_ROWS = 40

/** How many rows of a list the reader is NOT seeing.
 *
 *  TWO truncations stack on every list here and they are independent: the fold
 *  caps what it retains and reports the remainder, and this panel caps what it
 *  draws at `TABLE_ROWS`. Reporting only the fold's number understates the gap
 *  exactly when a list is longest -- a session with 40 tools and a fold cap of
 *  100 would have said "tools not detailed: 0" over a table showing 12 -- so the
 *  two are added. The reader's question is how many rows are missing, not which
 *  layer dropped them.
 */
function notShown(held: number, drawn: number, ...foldOmitted: number[]): number {
  return Math.max(0, held - drawn) + foldOmitted.reduce((a, b) => a + Math.max(0, b), 0)
}

/** A count of rows the reader cannot see, or nothing when none are missing.
 *
 *  Takes the rendered LINE rather than a key: a caller with a saturated fold
 *  counter needs a different sentence, and choosing it here would mean building
 *  the key from a variable -- which `[key-refs]` cannot resolve and `[added-lines]`
 *  reads as an untranslated literal. Every key stays spelled out at its call site.
 */
function NotShown({ n, line }: { n: number; line: string }) {
  if (n <= 0) return null
  return <div className="text-[10.5px] text-muted pt-1.5">{line}</div>
}

/* ── reading a projection value without trusting its shape ─────────────────
 * These values come off a route, so a field can be absent (a retention cut, an
 * older writer) — and ABSENT IS NOT ZERO, which is the posture the fold itself
 * takes. A reader is shown a dash for a number nobody reported rather than a 0
 * that reads as a measurement. */
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const int = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const obj = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter(x => x !== null && typeof x === 'object') as Record<string, unknown>[] : []

/** A count, or a dash when the value was never reported. */
const count = (v: unknown): string => {
  const n = num(v)
  return n === null ? '—' : fmtNumber(n)
}

/** A measured total, or a dash when NOTHING was measured into it.
 *
 *  `count` dashes a field that is ABSENT. This dashes one that is present and
 *  zero because no turn ever reported the measurement -- a distinction the fold
 *  draws for us by counting reporters separately from amounts (`credits_reported`,
 *  `tokens_reported`, `duration_reported`, and the same field per model). A turn
 *  the gateway synthesizes for a failure carries no usage numbers at all, so
 *  `credits: 0` beside `credits_reported: 0` does not mean the session was free;
 *  it means nobody said. Printing "0" states an amount the record never claimed,
 *  which is the same class of lie as showing a truncated list as if it were whole.
 *
 *  An absent reporter count reads as zero here, which dashes: a writer that did
 *  not send the field has not told us a measurement happened either. */
const measured = (v: unknown, reported: unknown): string =>
  int(reported) === 0 ? '—' : count(v)

/** Whether a fold reported this field at all -- a number or a non-empty string.
 *
 *  A stamp arrives as epoch MILLISECONDS, so a string-only test (`str(v)`) reads
 *  every one of them as absent: the field is there, the reader just cannot see it.
 *  A row guarded that way disappears on the common path rather than an extreme
 *  one, which is why presence and formatting are separate questions here. */
const present = (v: unknown): boolean => num(v) !== null || str(v) !== ''

/** A clock time from a stamp the folds carry.
 *
 *  Epoch milliseconds only, because that is the one shape that can arrive: the
 *  store's own reader drops an entry whose `time` is not an int, so no fold can
 *  hold a date string and a reader for one would be a branch nothing reaches. */
const at = (v: unknown): string => {
  const ms = num(v)
  return ms === null ? '—' : fmtTimeNumeric(ms)
}

/* ── presentation atoms ───────────────────────────────────────────────────── */

function Pill({ tone, children }: { tone: 'accent' | 'warn' | 'danger' | 'muted'; children: React.ReactNode }) {
  const cls = tone === 'accent'
    ? 'bg-accent-subtle text-accent'
    : tone === 'warn'
      ? 'bg-warn-subtle text-warn'
      : tone === 'danger'
        ? 'bg-danger-subtle text-danger'
        : 'bg-bg-hover text-muted'
  return <span className={`inline-flex items-center px-2 py-px rounded-full text-[10.5px] font-semibold ${cls}`}>{children}</span>
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 py-[3px] text-[11.5px]">
      {/* A hinted label carries its definition as the element's own title, so the
          word is explained where it is read rather than in a legend the reader
          has to find. Dotted underline because a tooltip nobody knows is there
          is the same as no tooltip. */}
      <span
        className={`text-muted min-w-[104px] shrink-0${hint ? ' underline decoration-dotted decoration-from-font cursor-help' : ''}`}
        title={hint}
      >
        {label}
      </span>
      <span className="text-text tabular-nums break-words min-w-0">{children}</span>
    </div>
  )
}

function Stat({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return (
    <div className="px-2.5 py-2 rounded-lg border border-border bg-[var(--bg-accent)]">
      <div className="text-[15px] font-semibold text-text-strong tabular-nums">{value}</div>
      {/* Same affordance the hinted rows carry: the definition sits on the thing it
          defines, and the dotted underline is what tells a reader it is there. */}
      <div
        className={`text-[10.5px] text-muted leading-snug${hint ? ' underline decoration-dotted decoration-from-font cursor-help' : ''}`}
        title={hint}
      >
        {label}
      </div>
    </div>
  )
}

function Section({
  id, title, summary, open, onToggle, children,
}: {
  id: string
  title: string
  summary: string
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className="border-b border-border">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={`crew-log-${id}`}
        className="flex items-center gap-2 w-full px-3 py-2.5 bg-transparent border-0 text-left cursor-pointer text-text hover:bg-bg-hover transition-colors"
      >
        <span className="text-muted shrink-0">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
        <span className="text-[12px] font-semibold text-text-strong">{title}</span>
        <span className="ml-auto text-[11px] text-muted tabular-nums truncate max-w-[55%]">{summary}</span>
      </button>
      {open && <div id={`crew-log-${id}`} className="px-3 pb-3">{children}</div>}
    </div>
  )
}

/* ── the five bodies ──────────────────────────────────────────────────────── */

function lifecycleTone(lifecycle: string): 'accent' | 'muted' {
  return lifecycle === 'open' ? 'accent' : 'muted'
}

function lifecycleLabel(lifecycle: string): string {
  if (lifecycle === 'open') return i18nT('pages.chat.crewLog.lifecycle_open')
  if (lifecycle === 'closed') return i18nT('pages.chat.crewLog.lifecycle_closed')
  return i18nT('pages.chat.crewLog.lifecycle_unknown')
}

/** A turn's stop reason in words, or the raw value when it is not one we know.
 *
 *  The fold passes the provider's own terminal reason through, so the set is open
 *  by construction: the three the store DECLARES get a translated word, and
 *  anything else is shown verbatim rather than mapped to a guess. */
function stopReasonLabel(reason: string): string {
  if (reason === 'end_turn') return i18nT('pages.chat.crewLog.stop_end_turn')
  if (reason === 'interrupted') return i18nT('pages.chat.crewLog.stop_interrupted')
  if (reason === 'failed') return i18nT('pages.chat.crewLog.stop_failed')
  return reason
}

/** The three decisions the gateway records, in words.
 *
 *  `approved`, `rejected` and `rejected_once` are what the writer stores, and a
 *  raw key read as a label beside translated rows reads as a guess rather than a
 *  count. An unrecognised value is shown as itself: a decision this panel has not
 *  learned yet is still worth seeing, and inventing a word for it would hide it. */
function decisionLabel(decision: string): string {
  if (decision === 'approved') return i18nT('pages.chat.crewLog.decision_approved')
  if (decision === 'rejected') return i18nT('pages.chat.crewLog.decision_rejected')
  if (decision === 'rejected_once') return i18nT('pages.chat.crewLog.decision_rejected_once')
  return decision
}

function StatusBody({ value }: { value: Record<string, unknown> }) {
  const turn = obj(value.turn)
  const dropped = obj(value.dropped)
  const lifecycle = str(value.lifecycle) || 'unknown'
  return (
    <>
      <Row label={i18nT('pages.chat.crewLog.field_lifecycle')}>
        <Pill tone={lifecycleTone(lifecycle)}>{lifecycleLabel(lifecycle)}</Pill>
      </Row>
      <Row label={i18nT('pages.chat.crewLog.field_agent')}>{str(value.agent) || '—'}</Row>
      <Row label={i18nT('pages.chat.crewLog.field_model')}>{str(value.model) || '—'}</Row>
      <Row label={i18nT('pages.chat.crewLog.field_opened')}>{at(value.opened_at)}</Row>
      {present(value.closed_at) && (
        <Row label={i18nT('pages.chat.crewLog.field_closed')}>
          {at(value.closed_at)}{str(value.close_reason) ? ` · ${str(value.close_reason)}` : ''}
        </Row>
      )}
      <Row label={i18nT('pages.chat.crewLog.field_turns')} hint={i18nT('pages.chat.crewLog.hint_turns')}>
        {i18nT('pages.chat.crewLog.turns_value', {
          completed: fmtNumber(int(value.turns_completed)),
          refused: fmtNumber(int(value.turns_refused)),
        })}
      </Row>
      {value.turn_open === true && (
        <Row label={i18nT('pages.chat.crewLog.field_turn_open')}>
          <Pill tone="warn">{i18nT('pages.chat.crewLog.turn_running', { turn: fmtNumber(int(turn.turn)) })}</Pill>
        </Row>
      )}
      <Row label={i18nT('pages.chat.crewLog.field_last_stop')}>
        {str(value.last_stop_reason) ? stopReasonLabel(str(value.last_stop_reason)) : '—'}
      </Row>
      {str(value.last_error) && (
        // An error text gets the shared notice, not a coloured span: that is what
        // carries the hand-off to the agent, and a session's last failure is
        // exactly the line a reader wants to ask about.
        <ErrorNotice message={str(value.last_error)} askAgent className="my-1.5" />
      )}
      <Row label={i18nT('pages.chat.crewLog.field_entries')} hint={i18nT('pages.chat.crewLog.hint_entries')}>{count(value.entries)}</Row>
      {int(dropped.count) > 0 && (
        <Row label={i18nT('pages.chat.crewLog.field_dropped')}>
          {i18nT('pages.chat.crewLog.dropped_value', { count: fmtNumber(int(dropped.count)) })}
        </Row>
      )}
    </>
  )
}

function UsageBody({ value }: { value: Record<string, unknown> }) {
  const tokens = obj(value.tokens)
  const turns = obj(value.turns)
  const compactions = obj(value.compactions)
  const context = obj(value.context)
  const byModel = obj(value.by_model)
  const modelNames = Object.entries(byModel)
  // The per-model table earns its space only when there is more than one model:
  // with one, every cell repeats a figure the header summary and the tiles above
  // already carry, and a reader is left checking whether three 0.93s are the same
  // number.
  const models = modelNames.length > 1
    ? modelNames.slice(0, TABLE_ROWS)
    : []
  const modelsHidden = notShown(modelNames.length > 1 ? modelNames.length : 0, models.length, int(value.models_omitted))
  const modelsHiddenLine = value.models_omitted_saturated === true
    ? i18nT('pages.chat.crewLog.models_omitted_floor', { count: fmtNumber(modelsHidden) })
    : i18nT('pages.chat.crewLog.models_omitted', { count: fmtNumber(modelsHidden) })
  return (
    <>
      <div className="grid grid-cols-2 gap-1.5">
        <Stat value={measured(value.credits, turns.credits_reported)} label={i18nT('pages.chat.crewLog.stat_credits', { turns: fmtNumber(int(turns.credits_reported)) })} />
        <Stat value={measured(tokens.total, turns.tokens_reported)} label={i18nT('pages.chat.crewLog.stat_tokens')} />
        <Stat value={int(turns.duration_reported) === 0 ? '—' : fmtElapsed(int(value.duration_ms))} label={i18nT('pages.chat.crewLog.stat_duration')} />
        <Stat
          value={count(compactions.count)}
          label={i18nT('pages.chat.crewLog.stat_compactions', { freed: fmtPercent((num(compactions.freed_pct) ?? 0) / 100) })}
          hint={i18nT('pages.chat.crewLog.hint_compactions')}
        />
      </div>
      <div className="mt-2">
        <Row label={i18nT('pages.chat.crewLog.field_tokens_input')}>{measured(tokens.input, turns.tokens_reported)}</Row>
        <Row label={i18nT('pages.chat.crewLog.field_tokens_output')}>{measured(tokens.output, turns.tokens_reported)}</Row>
        <Row label={i18nT('pages.chat.crewLog.field_tokens_cache_read')}>{measured(tokens.cache_read, turns.tokens_reported)}</Row>
        <Row label={i18nT('pages.chat.crewLog.field_tokens_cache_write')}>{measured(tokens.cache_write, turns.tokens_reported)}</Row>
        <Row label={i18nT('pages.chat.crewLog.field_context')}>
          {i18nT('pages.chat.crewLog.context_value', {
            tokens: fmtCompact(int(context.tokens)),
            blocks: fmtNumber(int(context.blocks)),
          })}
        </Row>
      </div>
      {models.length > 0 && (
        <table className="w-full border-collapse mt-2.5">
          <thead>
            <tr>
              <th className="text-left font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_model')}</th>
              <th className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_turns')}</th>
              <th className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_credits')}</th>
            </tr>
          </thead>
          <tbody>
            {models.map(([name, row]) => (
              <tr key={name}>
                <td className="py-[3px] border-b border-border text-text truncate max-w-[150px]">{name}</td>
                <td className="py-[3px] border-b border-border text-right tabular-nums">{count(obj(row).turns)}</td>
                <td className="py-[3px] border-b border-border text-right tabular-nums">{measured(obj(row).credits, obj(row).credits_reported)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {/* A single model is not "not detailed": its figures are in the tiles
          above, so only the rows this table actually withheld are counted. A
          spent dedup budget makes the fold's own count a floor, so the line has
          to say so rather than print a lower bound as exact. */}
      <NotShown n={modelsHidden} line={modelsHiddenLine} />
    </>
  )
}

/** A timeline moment's type, in words.
 *
 *  The record's own vocabulary is slash-separated (`turn/refused`), which sat oddly
 *  beside the stop reasons and decisions this panel already words. Only the types
 *  the store can WRITE are listed: `session/seeded` and the subagent types are in
 *  the fold's filter but are not declared entry types, so no session can hold one.
 *  An unrecognised type is shown as itself, the same policy the other two label
 *  helpers follow -- a moment this panel has not learned is still worth seeing.
 *
 *  The values are FULL literal keys, not suffixes to be joined at the call site.
 *  A key assembled from parts exists nowhere in the source, so the dead-key scan
 *  reads every one of these as unreferenced and the extractor cannot see them
 *  either -- which is what `dynamicKeys.test.ts` fails on, and it is right to. */
const MOMENT_LABEL_KEY: Record<string, string> = {
  'session/opened': 'pages.chat.crewLog.moment_session_opened',
  'session/closed': 'pages.chat.crewLog.moment_session_closed',
  'turn/started': 'pages.chat.crewLog.moment_turn_started',
  'turn/completed': 'pages.chat.crewLog.moment_turn_completed',
  'turn/refused': 'pages.chat.crewLog.moment_turn_refused',
  'compaction/applied': 'pages.chat.crewLog.moment_compaction_applied',
  'model/selected': 'pages.chat.crewLog.moment_model_selected',
  'write/dropped': 'pages.chat.crewLog.moment_write_dropped',
  'approval/requested': 'pages.chat.crewLog.moment_approval_requested',
  'approval/decided': 'pages.chat.crewLog.moment_approval_decided',
}

function momentLabel(type: string): string {
  const key = MOMENT_LABEL_KEY[type]
  return key ? i18nT(key) : type
}

function TimelineBody({ value }: { value: Record<string, unknown> }) {
  const moments = arr(value.moments)
  // Newest first: a reader opening the section is asking what just happened,
  // and the fold stores its window oldest-first.
  const shown = moments.slice(-TIMELINE_ROWS).reverse()
  const hidden = moments.length - shown.length
  if (moments.length === 0) {
    return <div className="text-[11.5px] text-muted py-1">{i18nT('pages.chat.crewLog.timeline_empty')}</div>
  }
  return (
    <>
      <div className="flex flex-col">
        {/* The right-hand number is the record's entry number, the same counter the
            footer reports as folded through. Unlabelled it was read as a guess. */}
        <div className="flex gap-2 pb-1 text-[10px] text-muted uppercase tracking-wide">
          <span className="ml-auto shrink-0">{i18nT('pages.chat.crewLog.timeline_entry_heading')}</span>
        </div>
        {shown.map(moment => (
          <div key={int(moment.seq)} className="flex gap-2 py-1 border-b border-border text-[11.5px]">
            <span className="text-muted tabular-nums min-w-[58px] shrink-0">{at(moment.time)}</span>
            <span className="text-text break-all min-w-0">{momentLabel(str(moment.type))}</span>
            <span className="ml-auto text-muted-strong text-[10.5px] tabular-nums shrink-0">{fmtNumber(int(moment.seq))}</span>
          </div>
        ))}
      </div>
      {(hidden > 0 || int(value.dropped) > 0) && (
        <div className="text-[10.5px] text-muted pt-1.5">
          {i18nT('pages.chat.crewLog.timeline_window', {
            hidden: fmtNumber(hidden),
            dropped: fmtNumber(int(value.dropped)),
          })}
        </div>
      )}
    </>
  )
}

function ToolsBody({ value }: { value: Record<string, unknown> }) {
  const byName = obj(value.by_name)
  const named = Object.entries(byName).sort((a, b) => int(obj(b[1]).calls) - int(obj(a[1]).calls))
  const rows = named.slice(0, TABLE_ROWS)
  const openCalls = arr(value.open_calls)
  const openDrawn = openCalls.slice(0, TABLE_ROWS)
  const toolsHidden = notShown(named.length, rows.length, int(value.names_omitted))
  const toolsHiddenLine = value.names_omitted_saturated === true
    ? i18nT('pages.chat.crewLog.tools_omitted_floor', { count: fmtNumber(toolsHidden) })
    : i18nT('pages.chat.crewLog.tools_omitted', { count: fmtNumber(toolsHidden) })
  // `open_dropped` counts calls the fold never retained, so they are in neither
  // this list nor the header's `unfinished` figure. A reader asking "is that all
  // of them" is owed those too.
  const openHidden = notShown(openCalls.length, openDrawn.length, int(value.open_calls_omitted), int(value.open_dropped))
  if (int(value.calls) === 0) {
    return <div className="text-[11.5px] text-muted py-1">{i18nT('pages.chat.crewLog.tools_empty')}</div>
  }
  return (
    <>
      <div className="grid grid-cols-2 gap-1.5">
        <Stat value={count(value.calls)} label={i18nT('pages.chat.crewLog.stat_calls')} />
        <Stat value={count(value.errors)} label={i18nT('pages.chat.crewLog.stat_errors')} />
      </div>
      <table className="w-full border-collapse mt-2.5">
        <thead>
          <tr>
            <th className="text-left font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_tool')}</th>
            <th className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_calls')}</th>
            <th className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_errors')}</th>
            <th className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_elapsed')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, row]) => (
            <tr key={name}>
              <td className="py-[3px] border-b border-border text-text truncate max-w-[140px]">{name}</td>
              <td className="py-[3px] border-b border-border text-right tabular-nums">{count(obj(row).calls)}</td>
              <td className="py-[3px] border-b border-border text-right tabular-nums">{count(obj(row).errors)}</td>
              <td className="py-[3px] border-b border-border text-right tabular-nums">{fmtElapsed(int(obj(row).elapsed_ms))}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <NotShown n={toolsHidden} line={toolsHiddenLine} />
      {openCalls.length > 0 && (
        <div className="mt-2.5">
          <div className="text-[10.5px] text-muted pb-1">{i18nT('pages.chat.crewLog.tools_open_heading')}</div>
          {openDrawn.map(call => (
            <div key={str(call.call_id)} className="flex gap-2 py-[3px] text-[11.5px] border-b border-border">
              <span className="text-text truncate min-w-0">{str(call.name) || '—'}</span>
              <span className="ml-auto text-muted tabular-nums shrink-0">{at(call.time)}</span>
            </div>
          ))}
          <NotShown
            n={openHidden}
            line={i18nT('pages.chat.crewLog.open_not_listed', { count: fmtNumber(openHidden) })}
          />
        </div>
      )}
    </>
  )
}

function ApprovalsBody({ value }: { value: Record<string, unknown> }) {
  const pending = arr(value.pending_requests)
  const pendingDrawn = pending.slice(0, TABLE_ROWS)
  const pendingHidden = notShown(pending.length, pendingDrawn.length, int(value.pending_omitted))
  const byDecision = obj(value.by_decision)
  if (int(value.requested) === 0) {
    return <div className="text-[11.5px] text-muted py-1">{i18nT('pages.chat.crewLog.approvals_empty')}</div>
  }
  return (
    <>
      <Row label={i18nT('pages.chat.crewLog.field_requested')}>{count(value.requested)}</Row>
      <Row label={i18nT('pages.chat.crewLog.field_decided')}>{count(value.decided)}</Row>
      {Object.entries(byDecision).map(([decision, n]) => (
        <Row key={decision} label={decisionLabel(decision)}>{count(n)}</Row>
      ))}
      {pending.length > 0 && (
        <div className="mt-2">
          <div className="text-[10.5px] text-muted pb-1">{i18nT('pages.chat.crewLog.approvals_pending_heading')}</div>
          {pendingDrawn.map(request => (
            <div key={str(request.approval_id)} className="flex gap-2 py-[3px] text-[11.5px] border-b border-border">
              <span className="text-text truncate min-w-0">{str(request.tool) || '—'}</span>
              <Pill tone="warn">{i18nT('pages.chat.crewLog.pending')}</Pill>
              <span className="ml-auto text-muted tabular-nums shrink-0">{at(request.time)}</span>
            </div>
          ))}
          <NotShown
            n={pendingHidden}
            line={i18nT('pages.chat.crewLog.pending_not_listed', { count: fmtNumber(pendingHidden) })}
          />
          {/* This section READS the record; the decision is taken on the card in
              the conversation, so say where rather than leave a row that looks
              like it should be clickable. */}
          <div className="text-[10.5px] text-muted pt-1.5">{i18nT('pages.chat.crewLog.approvals_pending_hint')}</div>
        </div>
      )}
    </>
  )
}

/* ── summaries drawn in a collapsed header ────────────────────────────────── */

function summaryFor(fold: CrewLogFold, value: Record<string, unknown>): string {
  if (fold === 'status') {
    return i18nT('pages.chat.crewLog.summary_status', {
      lifecycle: lifecycleLabel(str(value.lifecycle) || 'unknown'),
      turns: fmtNumber(int(value.turns_completed)),
    })
  }
  if (fold === 'usage') {
    // The same rule the tiles follow: a total nobody measured is a dash, not a
    // zero. A collapsed header is the ONE line a reader sees without opening the
    // fold, so "credits: 0" there is the most-read version of the claim.
    const turns = obj(value.turns)
    return i18nT('pages.chat.crewLog.summary_usage', {
      credits: int(turns.credits_reported) === 0
        ? '—'
        : fmtNumber(num(value.credits) ?? 0, { maximumFractionDigits: 2 }),
      tokens: int(turns.tokens_reported) === 0
        ? '—'
        : fmtCompact(int(obj(value.tokens).total)),
    })
  }
  if (fold === 'timeline') {
    return i18nT('pages.chat.crewLog.summary_timeline', { count: fmtNumber(arr(value.moments).length) })
  }
  if (fold === 'tools') {
    return i18nT('pages.chat.crewLog.summary_tools', {
      calls: fmtNumber(int(value.calls)),
      open: fmtNumber(int(value.open)),
    })
  }
  return i18nT('pages.chat.crewLog.summary_approvals', {
    requested: fmtNumber(int(value.requested)),
    pending: fmtNumber(int(value.pending)),
  })
}

const SECTION_TITLE_KEY: Record<CrewLogFold, string> = {
  status: 'pages.chat.crewLog.section_status',
  usage: 'pages.chat.crewLog.section_usage',
  timeline: 'pages.chat.crewLog.section_timeline',
  tools: 'pages.chat.crewLog.section_tools',
  approvals: 'pages.chat.crewLog.section_approvals',
}

/** Sections open on first render: the two that fit without scrolling. The three
 *  list folds stay closed — their headers already carry the count a reader is
 *  scanning for, and opening all five would put a 200-row feed above them. */
const OPEN_BY_DEFAULT: CrewLogFold[] = ['status', 'usage']

/* ── the section ──────────────────────────────────────────────────────────── */

export function CrewLogTab({ slot }: { slot: string }) {
  const { data, isLoading, error, refetch, isFetching } = useQuery<CrewLogRead>({
    queryKey: ['crew-log-projections', slot],
    queryFn: () => api.sessionCrewLogProjections(slot) as Promise<CrewLogRead>,
    enabled: !!slot,
    // The panel's body is unmounted while another tab is shown, so the turn-end
    // refetch below cannot fire for a turn that ran while it was away. Without
    // this, reopening the tab serves whatever the cache holds -- the fold from
    // before that turn -- and nothing later dislodges it, because the client's
    // default staleness never expires.
    refetchOnMount: 'always',
  })
  // TWO edges, because two different things append to this session's log and one
  // signal cannot see both. `selectSlotStreamState` falls when the session's own
  // turn stops streaming; `selectComposerBusy` stays true while spawned work runs
  // (chatSlice.ts:3923,3929) and falls when all of it drains, which is when a
  // `subagent/spawned` entry gets closed. Watching only the composer meant a turn
  // that finished alongside a long-running subagent showed its pre-turn fold for
  // as long as that subagent lived; watching only the stream would miss the
  // closures. Both edges mean "entries landed", and a duplicate refetch is one
  // read of one file.
  const turnRunning = useAppSelector(s => selectSlotStreamState(s, slot) !== 'idle')
  const busy = useAppSelector(s => selectComposerBusy(s, slot))
  const wasTurnRunning = useRef(turnRunning)
  const wasBusy = useRef(busy)
  useEffect(() => {
    // Falling edges only. A rising edge is a turn that has appended one entry and
    // not yet done the work a reader opened this panel to see.
    if ((wasTurnRunning.current && !turnRunning) || (wasBusy.current && !busy)) void refetch()
    wasTurnRunning.current = turnRunning
    wasBusy.current = busy
  }, [turnRunning, busy, refetch])

  const [openFolds, setOpenFolds] = useState<Set<CrewLogFold>>(() => new Set(OPEN_BY_DEFAULT))
  const toggle = useCallback((fold: CrewLogFold) => {
    setOpenFolds(prev => {
      const next = new Set(prev)
      if (next.has(fold)) next.delete(fold)
      else next.add(fold)
      return next
    })
  }, [])

  const folds = data?.folds
  const seq = useMemo(
    () => (folds ? Math.max(...CREW_LOG_FOLDS.map(fold => int(folds[fold]?.seq))) : 0),
    [folds],
  )

  const message = error ? (error instanceof Error ? error.message : String(error)) : null

  return (
    <div className="h-full flex flex-col bg-bg text-text" data-testid="crew-log-tab">
      <div className="flex-1 min-h-0 overflow-y-auto">
        <ErrorNotice message={message} askAgent className="m-3" />
        {isLoading && !data && (
          <div className="px-3 py-3 text-[11.5px] text-muted">{i18nT('pages.chat.crewLog.loading')}</div>
        )}
        {data && seq === 0 && (
          <div className="px-3 py-4 flex flex-col gap-1.5">
            {/* An id with no addressable unit is NOT the same as a session that
                recorded nothing: an idle reset leaves the record on disk under the
                retired ACP id, so telling that reader "nothing recorded" is false.
                The read says which case this is, so the panel can stop guessing. */}
            <div className="text-[12px] font-semibold text-text-strong">
              {i18nT(data.resolved
                ? 'pages.chat.crewLog.empty_title'
                : 'pages.chat.crewLog.unaddressable_title')}
            </div>
            <div className="text-[11.5px] text-muted leading-snug">
              {i18nT(data.resolved
                ? 'pages.chat.crewLog.empty_body'
                : 'pages.chat.crewLog.unaddressable_body')}
            </div>
          </div>
        )}
        {data && seq > 0 && CREW_LOG_FOLDS.map(fold => {
          const value = obj(folds?.[fold]?.value)
          return (
            <Section
              key={fold}
              id={fold}
              title={i18nT(SECTION_TITLE_KEY[fold])}
              summary={summaryFor(fold, value)}
              open={openFolds.has(fold)}
              onToggle={() => toggle(fold)}
            >
              {fold === 'status' && <StatusBody value={value} />}
              {fold === 'usage' && <UsageBody value={value} />}
              {fold === 'timeline' && <TimelineBody value={value} />}
              {fold === 'tools' && <ToolsBody value={value} />}
              {fold === 'approvals' && <ApprovalsBody value={value} />}
            </Section>
          )
        })}
      </div>
      <div className="flex items-center gap-2 px-3 py-1.5 border-t border-border bg-[var(--bg-accent)] text-[10.5px] text-muted">
        <span className="tabular-nums truncate">
          {seq > 0
            ? i18nT('pages.chat.crewLog.folded_through', { seq: fmtNumber(seq) })
            : i18nT('pages.chat.crewLog.folded_nothing')}
          {/* The writer queues an append and returns, so a fold taken as a turn
              ends can be behind the entries that turn wrote. Saying "up to date
              through entry N" for such a read would be the one claim in this
              footer that is not checkable from the record. */}
          {data && !data.writesDrained && ` · ${i18nT('pages.chat.crewLog.writes_pending')}`}
        </span>
        <button
          type="button"
          onClick={() => { void refetch() }}
          disabled={isFetching}
          className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer disabled:cursor-default disabled:opacity-60"
        >
          <RefreshCw size={11} className={isFetching ? 'animate-spin' : undefined} />
          {i18nT('pages.chat.crewLog.refresh')}
        </button>
      </div>
      {/* What this panel can and cannot answer, said once where the figures are.
          A record is addressed through the session's CURRENT unit, and a reset, a
          model switch or a compaction recycle starts a new one -- so a total here
          covers the record now in force, not the session's whole life. Left
          unsaid, a smaller total after a recycle reads as lost spend. */}
      <div className="px-3 pb-1.5 bg-[var(--bg-accent)] text-[10.5px] text-muted leading-snug">
        {i18nT('pages.chat.crewLog.scope_note')}
      </div>
    </div>
  )
}

export default CrewLogTab
