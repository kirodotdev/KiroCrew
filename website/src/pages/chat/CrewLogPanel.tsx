/**
 * The CREW LOG section of the chat right panel — the SPA consumer of the six
 * session folds `GET /api/sessions/{id}/crew-log/projection/{name}` serves.
 *
 * The backend folds; this renders (RFC NFR-2). Every number shown here is read
 * from a projection's `value`, and nothing is recomputed from entries: the panel
 * never reads the crew log itself, so it cannot disagree with the fold about the
 * same file.
 *
 * ONE query holds all six folds, because the panel presents them as one
 * section and a per-fold query would give the section six loading states and
 * six error states for a single refresh. Reads are scoped to the session the
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
 * footer reports the highest of the six, which is what makes "this value is
 * older than the log" observable instead of implied.
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { useAppSelector } from '../../store'
import { selectComposerBusy, selectSlotStreamState } from '../../store/chatSlice'
import { i18nT } from '../../i18n/t'
import { fmtCompact, fmtElapsed, fmtNumber, fmtPercent, fmtTimeNumeric } from '../../i18n/format'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'

/** The six folds, in the order the backend declares them (`PROJECTION_NAMES`). */
export const CREW_LOG_FOLDS = ['status', 'usage', 'timeline', 'tools', 'approvals', 'subagents'] as const
export type CrewLogFold = (typeof CREW_LOG_FOLDS)[number]

/** One fold's value at the seq it was folded through. */
export interface CrewLogProjection {
  session_id?: string
  name?: string
  seq: number
  value: Record<string, unknown>
}
export type CrewLogBundle = Record<CrewLogFold, CrewLogProjection>
/** What one read of the batch route answers: the six folds, plus the two things
 *  the folds themselves cannot say -- whether a unit was addressable for the id
 *  sent, and whether the writer owed entries as the fold was taken. */
/** Where `off_body`'s `{{link}}` points: the reference for what the crew log stores. */
const CREW_LOG_DOCS_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/reference/crew-log/README.md'

export type CrewLogRead = {
  folds: CrewLogBundle
  resolved: boolean
  writesDrained: boolean
  /** False when the gateway reports recording switched off. */
  recording: boolean
  /** The KIROCREW_CREW_LOG value that switched it off; empty when none was sent. */
  flagValue: string
  /** False when that value is not one of the switch-off spellings. */
  flagRecognised: boolean
  /** The `.env` the gateway reads, named in the switch-off instructions. */
  envFile: string
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
        {summary && (
          <span className="ml-auto text-[11px] text-muted tabular-nums truncate max-w-[55%]">{summary}</span>
        )}
      </button>
      {open && <div id={`crew-log-${id}`} className="px-3 pb-3">{children}</div>}
    </div>
  )
}

/* ── the six bodies ───────────────────────────────────────────────────────── */

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

/** The wording for each outcome the fold is known to emit.
 *
 *  A MAP of literals, not `` i18nT(`…outcome_${outcome}`) ``. An assembled key exists
 *  nowhere in the source, so the extractor cannot find it and `deadKeys.test.ts` reports
 *  all four of these as referenced nowhere -- then a pruning pass deletes them and the
 *  panel draws `pages.chat.crewLog.outcome_failed` where a word should be. Indexing a
 *  literal map keeps every key greppable and the lookup one expression, which is the
 *  shape `dynamicKeys.test.ts` prescribes.
 */
const OUTCOME_LABEL_KEY = {
  completed: 'pages.chat.crewLog.outcome_completed',
  failed: 'pages.chat.crewLog.outcome_failed',
  stopped: 'pages.chat.crewLog.outcome_stopped',
  unknown: 'pages.chat.crewLog.outcome_unknown',
} as const

function outcomeLabel(outcome: string): string {
  const key = OUTCOME_LABEL_KEY[outcome as keyof typeof OUTCOME_LABEL_KEY]
  // An OPEN enum: the fold passes the runtime's own word through rather than clamping
  // it, so a value this build has no wording for is drawn as itself instead of as a
  // missing translation key.
  return key ? i18nT(key) : outcome
}

function SubagentsBody({ value }: { value: Record<string, unknown> }) {
  const byId = obj(value.by_id)
  // Sorted by the entry seq the fold stamps on every row, which is dispatch order. The
  // fold renders `by_id` in that order already, but object-key iteration is the wrong
  // thing to trust for it: JSON revives integer-like keys in numeric order ahead of the
  // rest, so the moment a runtime spells an agent id as digits the table silently
  // reorders. The field is on every row, so ordering here costs one comparison.
  const children = Object.entries(byId).sort(
    ([, a], [, b]) => int(obj(a).seq_spawned) - int(obj(b).seq_spawned),
  )
  const rows = children.slice(0, TABLE_ROWS)
  const totals = obj(value.totals)
  // ONE reconciliation line, because the reader has one question: why are there fewer rows
  // than the header counts. It reports children DISPATCHED but not listed, which is
  // `omitted` plus whatever this table trimmed itself.
  //
  // Deliberately NOT `omitted + closed_unmatched`: those two are not disjoint. A dispatch
  // dropped past the cap whose closer arrives later raises BOTH, so adding them counts that
  // child twice. `closed_unmatched` is a fold-level diagnostic and stays in `totals` for a
  // reader of the projection; it answers a different question than this line.
  const undetailed = notShown(children.length, rows.length, int(value.omitted))
  // The unlisted children split by whether the reader can still GET to them, which is the
  // one thing the count alone does not say. A row this table trimmed is in the fold the
  // panel already fetched, so asking the agent for the projection produces it; a dispatch
  // the fold dropped past its retain cap is gone from the record and no reader can
  // recover it. The line below names the second number for that reason only -- pointing a
  // reader at the fold for children the fold never kept would be a dead end twice over.
  const dropped = Math.max(0, int(value.omitted))
  // How many of the unlisted ones are still going. Exact by construction: `running` comes
  // from the totals and the listed count comes from the rows, so the difference is the
  // running children absent from this table however they came to be absent. Without it the
  // header can read "running: 12" above a single running row with no way to follow it.
  const listedRunning = rows.filter(([, raw]) => !str(obj(raw).outcome)).length
  const runningUndetailed = Math.max(0, int(value.running) - listedRunning)
  // Children that CLOSED with no row of their own: a third population, and the only one this
  // section had no words for. `undetailed` cannot stand in for it -- that counts DISPATCHES,
  // and these have none -- so on a session where only closers reached the log it reads 0
  // while a child that genuinely ran, and billed, is named nowhere.
  const closedUnmatched = Math.max(0, int(totals.closed_unmatched))

  // "No subagents dispatched" needs BOTH counts to be zero, because `spawned` alone is not
  // the question. A child whose `subagent/spawned` append was abandoned in a `write/dropped`
  // marker while its closer landed leaves `spawned` at 0 and `closed_unmatched` at 1, with
  // real `ms` and `credits` billed against a child that genuinely ran -- and claiming nothing
  // was dispatched over a non-zero cost is the same class of lie as drawing a truncated list
  // as if it were whole. With only closers the table has no rows to draw, so the totals stand
  // alone and the reconciliation line accounts for them.
  if (int(totals.spawned) === 0 && int(totals.closed_unmatched) === 0) {
    return <div className="text-[11.5px] text-muted py-1">{i18nT('pages.chat.crewLog.subagents_empty')}</div>
  }
  return (
    <>
      {/* ONE tile. An aggregate subagent-credits tile used to sit beside this and was
          removed: it put a second money number on a panel that already shows Usage's
          credits, which then needed a label stating its coverage and a hint stating its
          relationship to that total -- two mechanisms whose only job was to explain a
          figure the per-child credits column already gives exactly. */}
      {/* No tile when nothing was dispatched. The true-empty state already hides it, and
          the closers-only state needs the same treatment for the same reason: a `0` an inch
          above "1 subagent finished" is a contradiction on its face, and a reader who
          cannot reconcile the two trusts neither. The sentence below carries that state on
          its own, and it says why the count is 0 -- no start reached the record. */}
      {int(totals.spawned) > 0 && (
        <Stat value={count(totals.spawned)} label={i18nT('pages.chat.crewLog.stat_spawned')} />
      )}
      {/* No rows, no table. A `thead` over an empty `tbody` reads as a list that failed to
          load, which is the shape the empty-state copy exists to avoid -- and this section
          reaches it by a second route: with only closers in the log there is real cost to
          report and no child to report it against, so the escape above deliberately does
          not fire and `by_id` is still empty. The reconciliation line below carries that
          case instead. */}
      {rows.length > 0 && (
      <table className="w-full border-collapse mt-2.5">
        <thead>
          <tr>
            <th className="text-left font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_agent')}</th>
            <th className="text-left font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_model')}</th>
            <th className="text-left font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_outcome')}</th>
            <th className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border">{i18nT('pages.chat.crewLog.col_elapsed')}</th>
            {/* The one hinted header on this table, because it is the one column that
                puts a SECOND money figure on a panel already showing Usage's credits,
                and a reader auditing spend cannot tell from two numbers whether one is
                inside the other. It is: since subagent closers began billing through
                `usage`, a child's charge is part of the session total rather than an
                addition to it. Same dotted-underline affordance the hinted rows and
                tiles carry. */}
            <th
              className="text-right font-normal text-muted text-[10.5px] py-[3px] border-b border-border underline decoration-dotted decoration-from-font cursor-help"
              title={i18nT('pages.chat.crewLog.col_credits_subagent_hint')}
            >
              {i18nT('pages.chat.crewLog.col_credits')}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([agentId, raw]) => {
            const row = obj(raw)
            const outcome = str(row.outcome)
            const charge = num(row.credits)
            const elapsed = num(row.ms)
            // A reason is an ERROR VALUE whatever the outcome says, so there is nothing for
            // the outcome to decide here. `on_subagent_completed` takes no reason at all, and
            // the only other closer passes `reason=info.error or ""` -- so a non-empty reason
            // reached this row from an error field and from nowhere else. A `stopped` row is
            // the case that proves it: an operator's stop whose kill then FAILS keeps the
            // neutral `stopped` outcome while `info.error` gains the kill failure, and gating
            // on the outcome put that string in a muted cell with no hand-off. That is the
            // shape `errors-use-error-notice` forbids, which tests where the value came from
            // rather than how it is drawn.
            const reason = str(row.reason)
            return (
              <Fragment key={agentId}>
              <tr>
                {/* Both cells truncate, and a truncated name with no recovery is a name the
                    reader cannot read at all: agent templates and model ids are long and
                    differ in their TAILS (`kirocrew-worker` / `kirocrew-writer-critic`), so
                    an ellipsis can hide the part that identifies the row. The full value is
                    the element's own `title`, which is where the rest of this panel puts a
                    value it had to shorten. */}
                <td className="py-[3px] border-b border-border text-text truncate max-w-[110px]" title={str(row.agent) || agentId}>{str(row.agent) || agentId}</td>
                <td className="py-[3px] border-b border-border text-muted truncate max-w-[80px]" title={str(row.model)}>{str(row.model) || '—'}</td>
                <td className="py-[3px] border-b border-border">
                  {outcome
                    ? <Pill tone={outcome === 'completed' ? 'accent' : outcome === 'stopped' ? 'muted' : 'danger'}>{outcomeLabel(outcome)}</Pill>
                    : <Pill tone="warn">{i18nT('pages.chat.crewLog.outcome_running')}</Pill>}
                </td>
                {/* A duration is absent in TWO cases, and neither is zero: a child still
                    running has no closer yet, and a closer writes `ms` only when it measured
                    one above zero -- crash-repair's closer writes none at all. So the cell
                    reads the value's presence, not the outcome's: `0.0s` for either would
                    present an absent measurement as a measured instant. */}
                <td className="py-[3px] border-b border-border text-right tabular-nums">
                  {elapsed === null ? '—' : fmtElapsed(elapsed)}
                </td>
                {/* THREE states, and all three are DRAWN differently: a charge; a child
                    that closed reporting none; and a child still running, which gets the
                    same dash the elapsed cell gets, because its cost is not knowable yet
                    rather than withheld. */}
                <td className="py-[3px] border-b border-border text-right tabular-nums text-muted">
                  {!outcome
                    ? '—'
                    : charge === null
                      ? i18nT('pages.chat.crewLog.credits_not_reported')
                      : fmtNumber(charge, { maximumFractionDigits: 2 })}
                </td>
              </tr>
              {/* The closer's own reason, on its own full-width row so it can be READ, and
                  always through ErrorNotice with the askAgent hand-off. ONE container, not a
                  choice: the value is an error field wherever it came from, and a read-only
                  status panel has no draft the hand-off could destroy. The outcome Pill one
                  line above still carries the neutral/danger distinction, which is the thing
                  the outcome actually decides. */}
              {reason && (
                <tr>
                  <td colSpan={5} className="pb-[3px] border-b border-border">
                    <ErrorNotice message={reason} askAgent variant="inline" />
                  </td>
                </tr>
              )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
      )}
      {(undetailed > 0 || closedUnmatched > 0) && (
        <div className="text-[10.5px] text-muted pt-1.5">
          {/* Complete SENTENCES, joined -- not clauses assembled into one. Each stands on
              its own in any word order a translator needs, and which ones apply depends on
              two independent facts (whether any child is beyond recovery, whether any is
              still going), so one string per combination would be four strings to say
              three things. */}
          {/* ONE of three whole sentences, never a base plus an addition. Composing them put
              "Ask in chat to see the full list." next to "2 of these were dropped ... cannot be
              recovered", which promises a list that cannot exist and leaves "these" bound to
              nothing a reader can identify. The mixed case therefore states both counts and
              what can be asked for in a single sentence. */}
          {/* Guarded on the count as well as on the block: `dropped >= undetailed` is true at
              0 >= 0, so a session whose only unlisted children are unmatched closers would
              otherwise open with "0 more counted above". */}
          {undetailed > 0 && (dropped >= undetailed
            ? i18nT('pages.chat.crewLog.subagents_undetailed_gone', { count: fmtNumber(undetailed) })
            : dropped > 0
              ? i18nT('pages.chat.crewLog.subagents_undetailed_dropped', {
                count: fmtNumber(undetailed),
                // The recoverable half, named so the sentence about it can stand alone.
                // Reading it off the two counts keeps the three numbers consistent on
                // screen; a reader who subtracts gets what the line already says.
                askable: fmtNumber(Math.max(0, undetailed - dropped)),
                dropped: fmtNumber(dropped),
              })
              : i18nT('pages.chat.crewLog.subagents_undetailed', { count: fmtNumber(undetailed) }))}
          {runningUndetailed > 0
            && ` ${i18nT(
              value.running_exact === false
                ? 'pages.chat.crewLog.subagents_undetailed_running_atleast'
                : 'pages.chat.crewLog.subagents_undetailed_running',
              { running: fmtNumber(runningUndetailed) },
            )}`}
          {/* Its own sentence, and a PLURAL key rather than a formatted number, because this
              is the one line on the panel that routinely renders at exactly 1 -- a closer
              whose dispatch never reached the file is a single lost append. It states the
              cost too: without it the closers-only session shows a `0` tile and nothing
              else, and the credits the fold billed have no per-child column to appear in. */}
          {closedUnmatched > 0
            && `${undetailed > 0 ? ' ' : ''}${i18nT(
              'pages.chat.crewLog.subagents_unmatched',
              { count: closedUnmatched },
            )}`}
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
  if (fold === 'approvals') {
    return i18nT('pages.chat.crewLog.summary_approvals', {
      requested: fmtNumber(int(value.requested)),
      pending: fmtNumber(int(value.pending)),
    })
  }
  // `spawned` rather than the row count: it is every child the session dispatched,
  // which is the number a reader is scanning this header for, and it exceeds the rows
  // by `omitted` on a session that dispatched past the retention cap.
  // `running` rather than `open.length`: the fold derives it from the totals, so it stays
  // exact once retention has dropped a dispatch, where the retained `open` list cannot.
  // Two spellings of one line, because `running` is sometimes a FLOOR rather than the
  // answer: with a dispatch dropped past the cap AND a closer that matched no row, the fold
  // subtracts that closer without being able to tell whether it belongs to a dropped
  // dispatch, so the count can be short by an unknown amount. Absent the flag the value is
  // treated as exact, which is what every writer before it emitted.
  // NO summary when the only thing that reached the log is a closer. Both counts would read
  // 0 -- truthfully, since nothing was recorded as dispatched and nothing is running -- an
  // inch above a sentence saying one child finished, and a reader cannot reconcile the two
  // from the header alone. The body's own sentence says it whole, including why a count
  // would be 0, so the header stays quiet rather than stating half of it. Same reason the
  // body drops its tile in this state.
  const subagentTotals = obj(value.totals)
  if (int(subagentTotals.spawned) === 0 && int(subagentTotals.closed_unmatched) > 0) return ''
  return i18nT(
    value.running_exact === false
      ? 'pages.chat.crewLog.summary_subagents_atleast'
      : 'pages.chat.crewLog.summary_subagents',
    {
      spawned: fmtNumber(int(subagentTotals.spawned)),
      open: fmtNumber(int(value.running)),
    },
  )
}

const SECTION_TITLE_KEY: Record<CrewLogFold, string> = {
  status: 'pages.chat.crewLog.section_status',
  usage: 'pages.chat.crewLog.section_usage',
  timeline: 'pages.chat.crewLog.section_timeline',
  tools: 'pages.chat.crewLog.section_tools',
  approvals: 'pages.chat.crewLog.section_approvals',
  subagents: 'pages.chat.crewLog.section_subagents',
}

/** Sections open on first render: the two that fit without scrolling. The four
 *  list folds stay closed — their headers already carry the count a reader is
 *  scanning for, and opening all six would put a 200-row feed above them. */
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
  // (selectors.ts in store/chat) and falls when all of it drains, which is when a
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
  // Recording switched off is said whenever the gateway reports it, above any entries
  // an earlier run wrote: those stay readable, but nothing new is being added to them.
  const recordingOff = data?.recording === false
  // With recording off the footer leads with that, and the scope note -- which log "this
  // chat is writing now" -- is hidden, because nothing is being written. Entries an
  // earlier run saved are still on screen, so their watermark stays beside the status.
  const hideRecordClaims = recordingOff && seq === 0
  // A chat whose current id names no record has nothing to be "up to date" with, so
  // its footer carries only the refresh; the body says why there is nothing to show.
  const unaddressable = !!data && !recordingOff && seq === 0 && data.resolved === false

  return (
    <div className="h-full flex flex-col bg-bg text-text" data-testid="crew-log-tab">
      <div className="flex-1 min-h-0 overflow-y-auto">
        <ErrorNotice message={message} askAgent className="m-3" />
        {isLoading && !data && (
          <div className="px-3 py-3 text-[11.5px] text-muted">{i18nT('pages.chat.crewLog.loading')}</div>
        )}
        {data && recordingOff && (
          <div
            className="mx-3 my-3 px-2.5 py-2 flex flex-col gap-1.5 rounded border border-warn/30 bg-warn-subtle"
            role="status"
            data-testid="crew-log-off"
          >
            <div className="text-[12px] font-semibold flex items-center gap-1.5 text-warn">
              <AlertTriangle size={13} aria-hidden="true" className="shrink-0" />
              {i18nT('pages.chat.crewLog.off_title')}
            </div>
            {/* The consequence first, and it depends on what is on screen: with entries
                below, only NEW messages go unsaved. */}
            <div className="text-[11.5px] leading-snug font-medium text-text">
              {i18nT(seq > 0 ? 'pages.chat.crewLog.off_lead_entries' : 'pages.chat.crewLog.off_lead')}
            </div>
            <div className="text-[11.5px] leading-snug text-text">
              {splitOnPlaceholder(
                !data.flagValue
                  ? i18nT('pages.chat.crewLog.off_body_unknown', { file: data.envFile })
                  : i18nT(data.flagRecognised
                    ? 'pages.chat.crewLog.off_body'
                    : 'pages.chat.crewLog.off_body_unrecognised', { value: data.flagValue, file: data.envFile }),
                'link',
              ).map((part, i) =>
                part === null ? (
                  <a
                    key="link"
                    href={CREW_LOG_DOCS_URL}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--accent)] hover:underline"
                  >
                    {i18nT('pages.chat.crewLog.off_link')}
                  </a>
                ) : (
                  <span key={i}>{part}</span>
                ))}
            </div>
          </div>
        )}
        {data && seq === 0 && !recordingOff && (
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
              {fold === 'subagents' && <SubagentsBody value={value} />}
            </Section>
          )
        })}
      </div>
      <div className="flex items-center gap-2 px-3 py-1.5 border-t border-border bg-[var(--bg-accent)] text-[10.5px] text-muted">
        {hideRecordClaims && (
          <span className="truncate" data-testid="crew-log-footer-off">
            {i18nT('pages.chat.crewLog.footer_off')}
          </span>
        )}
        {!hideRecordClaims && !unaddressable && <span
          className="tabular-nums truncate"
          data-testid={recordingOff ? 'crew-log-footer-off' : undefined}
        >
          {seq > 0
            ? i18nT(
              recordingOff ? 'pages.chat.crewLog.footer_off_through' : 'pages.chat.crewLog.folded_through',
              { seq: fmtNumber(seq) },
            )
            : i18nT('pages.chat.crewLog.folded_nothing')}
          {/* The writer queues an append and returns, so a fold taken as a turn
              ends can be behind the entries that turn wrote. Saying "up to date
              through entry N" for such a read would be the one claim in this
              footer that is not checkable from the record. */}
          {data && !data.writesDrained && ` · ${i18nT('pages.chat.crewLog.writes_pending')}`}
        </span>}
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
      {!recordingOff && (
        <div className="px-3 pb-1.5 bg-[var(--bg-accent)] text-[10.5px] text-muted leading-snug">
          {i18nT('pages.chat.crewLog.scope_note')}
        </div>
      )}
    </div>
  )
}

export default CrewLogTab
