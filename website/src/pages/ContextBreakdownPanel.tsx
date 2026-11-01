import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { api } from '../api/client'
import { ACP_BACKEND_KIRO, acpBackendName } from '../api/acpBackend'
import { contextTraceKey } from '../hooks/useWebSocket'
import ErrorNotice from '../components/ErrorNotice'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { SubagentActivity } from '../types'
import { breakdownOf, Headline, headlineFigure, MESSAGE_COLOR, TurnBreakdown, type CrewCategory, type LatestTurn } from './ContextEngineBreakdown'
import { SessionBreakdownTree } from './SessionBreakdownTree'
import { CATEGORY_FILL } from './contextSourceColors'

/**
 * What one kept turn sent, by block name, as GET /api/telemetry/context-trace returns
 * it for the Session Breakdown tree. Every block size is in CHARS.
 */
export interface ContextTurn {
  phase: string
  blocks: Record<string, number>
  total_chars: number
  /** The turn's EXACT 1-based position in the whole session history, assigned by the
   *  backend fold before any truncation. Shown as the turn number directly: it stays
   *  true no matter how many older turns the fold dropped or this day view excluded,
   *  where a count applied to the array index would not. */
  ordinal: number
  /** Whether this row is the session's real start rather than a later rebuild, as the
   *  backend fold marked it. Absent on a gateway older than the field. */
  first_start?: boolean
}

/** One turn of the trend ring: this app's five sums and the rest of the context. */
export interface TrendRow {
  turn: number
  ours: Record<CrewCategory, number>
  loop: number
  /** The turn's own occupancy reading in TOKENS, the harness total; 0 when it
   *  reported none. Absent on an older gateway. */
  used?: number
  /** The row's version: it changes whenever the fold writes the row again (a turn
   *  sent again), whatever it holds. Absent on an older gateway. */
  version?: number
  /** The turn whole, in `latest`'s shape. The gateway sends none: a picked past turn
   *  is read on demand (`telemetryContextTurn`). A fixture trace may carry it. */
  detail?: LatestTurn
  /** On the start row: the size its own panel heads with. */
  figure?: { n: number; unit: 'chars' | 'tokens' } | null
}

/** A past turn read on demand: GET /api/telemetry/context-trace?slot=..&turn=<ordinal>. */
export interface PastTurnRead {
  ordinal: number
  detail: LatestTurn | null
  loading: boolean
  error: string | null
}

export interface ContextTrace {
  slot: string
  turns: ContextTurn[]
  totals: Record<string, number>
  injected_chars: number
  user_chars: number
  /** Occupancy pair in TOKENS, read by the Session Breakdown tree. */
  peak_context_used: number
  context_window: number
  window_days: number
  /** Whether the gateway is recording at all. `false` means nothing will ever arrive
   *  while the switch stays off, so the empty state must not promise otherwise. Absent
   *  on a payload from a gateway older than the field, which reads as recording. */
  recording?: boolean
  /** The `.env` file the gateway reads, sent only while recording is off. */
  env_file?: string
  /** The newest turn's one breakdown; null before any turn. Absent on an older gateway. */
  latest?: LatestTurn | null
  /** The newest 30 turns, oldest first. */
  trend?: TrendRow[]
  /** The newest session-start turn, listed above the chart. */
  start?: TrendRow | null
  /** Turns before the first trend row. */
  earlier?: number
  /** The fold revision the trace was read at. */
  revision?: number
  turn_count?: number
  /** The characters-per-token ratio the fold's estimate used. */
  chars_per_token?: number | null
}

/** The user's own text, and the labels the backend groups under one bucket. */
export const USER_LABEL = 'your_message'

// The five per-turn boilerplate blocks are merged into one display bucket: each
// is a few hundred chars and the fact worth showing is that they REPEAT, not
// their individual sizes. Mirrors EVERY_TURN_LABELS in context_blocks.py.
export const EVERY_TURN_MEMBERS: ReadonlySet<string> = new Set([
  'surface',
  'working_folder',
  'request_header',
  'reply_format_rules',
  'user_display',
])
const EVERY_TURN_KEY = 'every_turn'

/** The five plain-language categories the chart stacks, bottom to top. */
export type Category = 'message' | 'memory' | 'rules' | 'skills' | 'other'
export const CATEGORIES: readonly Category[] = ['message', 'memory', 'rules', 'skills', 'other']

/**
 * Backend block label -> chart category. A label absent here lands in `other`,
 * so an unrecognised block is never dropped from a turn's total. The
 * every-turn members and `unclassified` are deliberately absent: they are
 * "other" by definition.
 */
export const CATEGORY_OF: Readonly<Record<string, Category>> = {
  [USER_LABEL]: 'message',
  memory: 'memory',
  semantic_memory: 'memory',
  episodic_memory: 'memory',
  task_facts: 'memory',
  lessons: 'rules',
  critical_rules: 'rules',
  agent_instructions: 'rules',
  response_preferences: 'rules',
  skill_index: 'skills',
  skill_hint: 'skills',
  loaded_skill: 'skills',
}

export function categoryOf(label: string): Category {
  return CATEGORY_OF[label] ?? 'other'
}

/** Sum a turn's blocks into the five categories. Every category is present (zero when empty). */
export function categorise(blocks: Record<string, number>): Record<Category, number> {
  const out: Record<Category, number> = { message: 0, memory: 0, rules: 0, skills: 0, other: 0 }
  for (const [label, chars] of Object.entries(blocks)) out[categoryOf(label)] += chars
  return out
}

/** The newest turns the chart draws; older ones are summarised as a count. */
export const MAX_CHART_TURNS = 30

// Stable label -> catalog-key map. Anything absent is humanised from its id at
// render time, so the long tail of rare blocks needs no catalog entry.
const BLOCK_KEY: Record<string, string> = {
  your_message: 'pages.contextBreakdown.block_your_message',
  memory: 'pages.contextBreakdown.block_memory',
  agent_instructions: 'pages.contextBreakdown.block_agent_instructions',
  lessons: 'pages.contextBreakdown.block_lessons',
  semantic_memory: 'pages.contextBreakdown.block_semantic_memory',
  episodic_memory: 'pages.contextBreakdown.block_episodic_memory',
  task_facts: 'pages.contextBreakdown.block_task_facts',
  skill_index: 'pages.contextBreakdown.block_skill_index',
  skill_hint: 'pages.contextBreakdown.block_skill_hint',
  loaded_skill: 'pages.contextBreakdown.block_loaded_skill',
  critical_rules: 'pages.contextBreakdown.block_critical_rules',
  response_preferences: 'pages.contextBreakdown.block_response_preferences',
  [EVERY_TURN_KEY]: 'pages.contextBreakdown.block_every_turn',
  unclassified: 'pages.contextBreakdown.block_unclassified',
}

/** Humanise a block id for the long tail: `hook_context` -> `Hook context`. */
function humanise(label: string): string {
  const spaced = label.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

export function blockName(label: string): string {
  const key = BLOCK_KEY[label]
  return key ? i18nT(key) : humanise(label)
}

const CATEGORY_KEY: Record<Category, string> = {
  message: 'pages.contextBreakdown.cat_message',
  memory: 'pages.contextBreakdown.cat_memory',
  rules: 'pages.contextBreakdown.cat_rules',
  skills: 'pages.contextBreakdown.cat_skills',
  other: 'pages.contextBreakdown.cat_other',
}

/** Merge the every-turn members into one bucket; every other label passes through. */
export function groupBlocks(blocks: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [label, chars] of Object.entries(blocks)) {
    const key = EVERY_TURN_MEMBERS.has(label) ? EVERY_TURN_KEY : label
    out[key] = (out[key] ?? 0) + chars
  }
  return out
}

const fmtN = (n: number): string => fmtNumber(Math.round(n))

/**
 * Y-axis ticks: a "nice" step (1, 2 or 5 times a power of ten) chosen so
 * the axis carries three or four gridlines, and a ceiling that is a whole
 * multiple of it. The top tick is what the chart scales against.
 */
export function niceTicks(max: number): number[] {
  if (max <= 0) return [0, 1]
  const rough = max / 3
  const mag = 10 ** Math.floor(Math.log10(rough))
  const unit = rough / mag
  const step = (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * mag
  const ticks: number[] = []
  for (let v = 0; v < max + step; v += step) ticks.push(Math.round(v * 1e6) / 1e6)
  return ticks
}

const CHART_HEIGHT = 260
const CHART_FALLBACK_WIDTH = 520
const MARGIN = { top: 22, right: 28, bottom: 44, left: 60 }
/** Horizontal room one x-axis label needs; the label stride derives from it. */
const LABEL_MIN_PX = 44
/** Below this a per-turn hit column is too thin to aim at; one plot-wide surface takes over. */
const HIT_COLUMN_MIN_PX = 12
/** Below this the selected turn's value label is dropped: the detail's big number repeats it. */
const VALUE_LABEL_MIN_WIDTH = 480

interface ChartTurn {
  /** 1-based index WITHIN the retained window. The selection key and the array
   *  index (`trace.turns[n - 1]`), NOT what the user sees: turns dropped by the
   *  backend before the window are not in the array, so this is not the turn's
   *  true ordinal. */
  n: number
  /** The turn's TRUE ordinal for display, straight from the backend row's own
   *  `ordinal`. Shown to the user; never used to index
   *  the window-local array. */
  displayN: number
  total: number
  /** Band sizes by layer: the five categories and, on the trend chart, `loop`. */
  cats: Record<string, number>
  isStart: boolean
  /** The session's real start, not a rebuild. */
  isFirstStart: boolean
  /** A token column whose turn reported no total: drawn hatched, with no rest band. */
  missing?: boolean
}

/**
 * Which x-axis labels to draw for `count` turns across `plotWidth` px: every
 * `stride`-th turn, plus the selected and the last turn, minus a strided
 * neighbour that would sit on top of either of those two.
 */
export function axisLabelIndices(count: number, plotWidth: number, selectedIdx: number): number[] {
  const maxLabels = Math.max(1, Math.floor(plotWidth / LABEL_MIN_PX))
  const stride = Math.max(1, Math.ceil(count / maxLabels))
  const last = count - 1
  const out: number[] = []
  for (let i = 0; i < count; i++) {
    if (i === selectedIdx || i === last) {
      out.push(i)
      continue
    }
    if (i % stride !== 0) continue
    if (Math.abs(i - selectedIdx) < stride || last - i < stride) continue
    out.push(i)
  }
  return out
}

/**
 * The stacked area: one polygon per category, separated by card-coloured
 * hairlines, a foreground line along the top, and a dashed marker on the
 * selected turn. Turns are keyboard-reachable buttons laid over the plot area,
 * so the chart itself stays a plain `role="img"` picture.
 */
function StackedArea({
  turns,
  selected,
  selectedDisplayN,
  onSelect,
  width: fixedWidth,
  layers = CATEGORIES,
  fills = CATEGORY_FILL,
  interactive = true,
  tokens = false,
  onLatest,
}: {
  turns: ChartTurn[]
  selected: number
  /** The selected turn's TRUE ordinal, resolved by the PARENT against the whole
   *  retained set. The chart cannot derive it: `turns` here holds only the rows it
   *  draws, so a session-start row selected ABOVE the chart is absent from it and
   *  `selected` is that row's window-local `n`, which is not what the user is shown. */
  selectedDisplayN: number
  onSelect: (n: number) => void
  /** Overrides the measured width (capture harnesses and tests). */
  width?: number
  /** Bands bottom to top, and their fills. */
  layers?: readonly string[]
  fills?: Readonly<Record<string, string>>
  /** False draws the picture only: no turn buttons. */
  interactive?: boolean
  /** The values are estimated tokens rather than characters. */
  tokens?: boolean
  /** Escape or Home on a column: back to the newest turn, which may not be a column. */
  onLatest?: () => void
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const buttonRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [measured, setMeasured] = useState(CHART_FALLBACK_WIDTH)
  const width = fixedWidth ?? measured

  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el || fixedWidth !== undefined || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const w = el.getBoundingClientRect().width
      if (w > 0) setMeasured(w)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [fixedWidth])

  const count = turns.length
  const plotLeft = MARGIN.left
  const plotRight = Math.max(plotLeft + 1, width - MARGIN.right)
  const plotWidth = plotRight - plotLeft
  const plotTop = MARGIN.top
  const plotBottom = CHART_HEIGHT - MARGIN.bottom
  const maxTotal = Math.max(...turns.map(t => t.total), 0)
  const ticks = niceTicks(maxTotal)
  const yMax = ticks[ticks.length - 1]
  const y = (v: number) => plotBottom - (v / yMax) * (plotBottom - plotTop)
  // A single turn has no run along the x axis, so its band spans the whole plot.
  const xAt = (i: number) => (count === 1 ? (plotLeft + plotRight) / 2 : plotLeft + (i * plotWidth) / (count - 1))
  const polyXs = count === 1 ? [plotLeft, plotRight] : turns.map((_, i) => xAt(i))
  const valuesOf = (pick: (t: ChartTurn) => number) =>
    count === 1 ? [pick(turns[0]), pick(turns[0])] : turns.map(pick)

  // Columns with no reported total break the rest band and the top line into runs,
  // so a missing total reads as a gap, never as a drop to Kiro Crew's own blocks.
  const missingAt = count > 1 ? turns.map(t => !!t.missing) : [false, false]
  const runs: number[][] = []
  missingAt.forEach((gap, i) => {
    if (gap) return
    if (i > 0 && !missingAt[i - 1] && runs.length) runs[runs.length - 1].push(i)
    else runs.push([i])
  })
  const columnHalf = count > 1 ? plotWidth / (count - 1) / 2 : plotWidth / 2
  // A run of one column still gets a visible sliver of width.
  const runXs = (run: number[]) =>
    run.length === 1 && count > 1
      ? [Math.max(plotLeft, polyXs[run[0]] - columnHalf / 2), Math.min(plotRight, polyXs[run[0]] + columnHalf / 2)]
      : run.map(i => polyXs[i])
  const runVals = (run: number[], vals: number[]) => (run.length === 1 && count > 1 ? [vals[run[0]], vals[run[0]]] : run.map(i => vals[i]))
  const pts = (xs: number[], vals: number[]) => vals.map((v, k) => `${xs[k]},${y(v)}`)
  const hasGap = missingAt.some(Boolean)

  let bottom = valuesOf(() => 0)
  const bands = layers.map(cat => {
    const top = bottom.map((v, i) => v + valuesOf(t => t.cats[cat] ?? 0)[i])
    const pieces = hasGap && cat === 'loop' ? runs : [missingAt.map((_, i) => i)]
    const polygons = pieces.map(run => {
      const xs = hasGap && cat === 'loop' ? runXs(run) : polyXs
      const t = hasGap && cat === 'loop' ? runVals(run, top) : top
      const b = hasGap && cat === 'loop' ? runVals(run, bottom) : bottom
      return { polygon: [...pts(xs, t), ...pts(xs, b).reverse()].join(' '), line: pts(xs, t).join(' ') }
    })
    const crewTop = bottom
    bottom = top
    return { cat, polygons, crewTop }
  })
  const totalLines = hasGap ? runs.map(run => pts(runXs(run), runVals(run, bottom)).join(' ')) : [pts(polyXs, bottom).join(' ')]
  // The hatched columns, from Kiro Crew's own blocks up to the top of the plot.
  const crewTops = bands.find(b => b.cat === 'loop')?.crewTop ?? bottom
  const hatches = missingAt
    .map((gap, i) => (gap ? i : -1))
    .filter(i => i >= 0)
    .map(i => {
      const left = Math.max(plotLeft, xAt(i) - columnHalf)
      const right = Math.min(plotRight, xAt(i) + columnHalf)
      return { i, x: left, w: Math.max(1, right - left), top: plotTop, h: Math.max(0, y(crewTops[i]) - plotTop) }
    })

  // -1 when the selected turn is a session-start row above the chart: then no
  // chart column is marked or pressed, and the keyboard enters at the newest.
  const selectedIdx = turns.findIndex(t => t.n === selected)
  const sel = selectedIdx >= 0 ? turns[selectedIdx] : undefined
  const focusIdx = selectedIdx >= 0 ? selectedIdx : count - 1
  const selX = xAt(focusIdx)
  const selY = y(sel?.total ?? 0)
  // The value label sits above the highest point of the top line within one
  // column either side, so a rising neighbour never runs through it.
  const localMax = Math.max(
    ...turns.slice(Math.max(0, focusIdx - 1), Math.min(count, focusIdx + 2)).map(t => t.total),
  )
  const labelY = y(localMax) - 12
  const showValueLabel = sel !== undefined && width >= VALUE_LABEL_MIN_WIDTH
  // Keep the value label inside the plot when the selected turn sits at an edge.
  const labelAnchor = focusIdx === count - 1 && count > 1 ? 'end' : focusIdx === 0 && count > 1 ? 'start' : 'middle'
  const labelX = labelAnchor === 'end' ? selX - 10 : labelAnchor === 'start' ? selX + 10 : selX
  const labelled = new Set(axisLabelIndices(count, plotWidth, focusIdx))

  // Arrow keys move from the button that received them, so focus never jumps
  // to the far end of the chart; the group is one tab stop (roving tabindex).
  const move = (from: number, delta: number) => {
    const next = Math.min(count - 1, Math.max(0, from + delta))
    if (next === from) return
    onSelect(turns[next].n)
    buttonRefs.current[next]?.focus()
  }
  const onArrow = (e: KeyboardEvent<HTMLButtonElement>, from: number) => {
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      move(from, -1)
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      move(from, 1)
    } else if ((e.key === 'Escape' || e.key === 'Home') && onLatest) {
      e.preventDefault()
      onLatest()
    }
  }

  const columnWidth = plotWidth / Math.max(1, count - 1)
  const hitLeft = (i: number) => (count === 1 ? plotLeft : xAt(i) - columnWidth / 2)
  const hitWidth = count === 1 ? plotWidth : columnWidth
  // Thin columns cannot be aimed at, so one surface over the whole plot takes
  // the pointer and hands the nearest turn to the same selection; the per-turn
  // buttons stay for the keyboard and assistive tech.
  const pointerSurface = count > 1 && columnWidth < HIT_COLUMN_MIN_PX
  const nearestTurn = (clientX: number): number => {
    const left = wrapRef.current?.getBoundingClientRect().left ?? 0
    const i = Math.round((clientX - left - plotLeft) / columnWidth)
    return turns[Math.min(count - 1, Math.max(0, i))].n
  }

  return (
    <div ref={wrapRef} className="relative w-full" style={{ height: CHART_HEIGHT }}>
      <svg
        width={width}
        height={CHART_HEIGHT}
        className="block"
        role="img"
        aria-label={i18nT('pages.contextBreakdown.chart_aria', { n: fmtN(selectedDisplayN) })}
        style={{ fontSize: 11 }}
      >
        {ticks.map(v => (
          <g key={v}>
            <line x1={plotLeft} x2={plotRight} y1={y(v)} y2={y(v)} stroke="var(--border)" />
            <text x={plotLeft - 10} y={y(v) + 4} textAnchor="end" fill="var(--muted)" className="tabular-nums">
              {fmtN(v)}
            </text>
          </g>
        ))}
        <defs>
          <pattern id="ctx-no-total" width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1={0} y1={0} x2={0} y2={6} stroke="var(--muted)" strokeWidth={1.5} strokeOpacity={0.5} />
          </pattern>
        </defs>
        {bands.map(layer => (
          <g key={layer.cat} data-category={layer.cat}>
            {layer.polygons.map((piece, k) => (
              <g key={k}>
                <polygon points={piece.polygon} fill={fills[layer.cat]} fillOpacity={0.83} />
                <polyline points={piece.line} fill="none" stroke="var(--card)" strokeWidth={1} />
              </g>
            ))}
          </g>
        ))}
        {hatches.map(h => (
          <rect key={h.i} x={h.x} y={h.top} width={h.w} height={h.h} fill="url(#ctx-no-total)" data-testid="no-total-column" data-turn={turns[h.i].n} />
        ))}
        {totalLines.map((line, k) => (
          <polyline key={k} points={line} fill="none" stroke="var(--card-fg, var(--text-strong))" strokeWidth={1.5} />
        ))}
        {sel ? (
          <>
            <line
              x1={selX}
              x2={selX}
              y1={plotTop - 4}
              y2={plotBottom + 4}
              stroke="var(--card-fg, var(--text-strong))"
              strokeWidth={1}
              strokeDasharray="3 4"
              data-testid="selected-turn-marker"
            />
            <circle cx={selX} cy={selY} r={4} fill="var(--card)" stroke="var(--card-fg, var(--text-strong))" strokeWidth={2} />
          </>
        ) : null}
        {sel && showValueLabel ? (
          <text
            x={labelX}
            y={labelY}
            textAnchor={labelAnchor}
            fill="var(--card-fg, var(--text-strong))"
            fontWeight={600}
            className="tabular-nums"
            data-testid="selected-turn-value"
          >
            {fmtN(sel.total)}
          </text>
        ) : null}
        {turns.map((t, i) =>
          labelled.has(i) ? (
            <text
              key={t.n}
              x={xAt(i)}
              y={plotBottom + 24}
              textAnchor="middle"
              fill={i === selectedIdx ? 'var(--card-fg, var(--text-strong))' : 'var(--muted)'}
              fontWeight={i === selectedIdx ? 600 : 400}
              data-axis-label={t.n}
            >
              {i18nT('pages.contextBreakdown.axis_turn_n', { n: fmtN(t.displayN) })}
            </text>
          ) : null,
        )}
      </svg>
      {/* Transparent hit columns: one real button per turn so selection is
          clickable, focusable and arrow-key navigable. */}
      {interactive ? <div className="absolute inset-y-0 left-0 right-0 cursor-pointer" role="group" aria-label={i18nT('pages.contextBreakdown.turn_picker')}>
        {pointerSurface ? (
          <button
            type="button"
            tabIndex={-1}
            aria-hidden="true"
            data-testid="turn-pointer-surface"
            className="absolute inset-y-0 appearance-none bg-transparent border-0 p-0 m-0 cursor-pointer"
            style={{ left: plotLeft, width: plotWidth }}
            onClick={e => onSelect(nearestTurn(e.clientX))}
          />
        ) : null}
        {turns.map((t, i) => (
          <button
            key={t.n}
            ref={el => {
              buttonRefs.current[i] = el
            }}
            type="button"
            tabIndex={i === focusIdx ? 0 : -1}
            aria-pressed={i === selectedIdx}
            aria-label={
              tokens
                ? i18nT('pages.contextBreakdown.turn_button_tokens', { n: fmtN(t.displayN), tokens: fmtN(t.total) })
                : i18nT('pages.contextBreakdown.turn_button', { n: fmtN(t.displayN), chars: fmtN(t.total) })
            }
            data-turn={t.n}
            className={`absolute inset-y-0 appearance-none bg-transparent border-0 p-0 m-0 cursor-pointer rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] ${
              pointerSurface ? 'pointer-events-none' : 'hover:bg-[var(--card-hl)]'
            }`}
            style={{ left: hitLeft(i), width: hitWidth }}
            onClick={() => onSelect(t.n)}
            onKeyDown={e => onArrow(e, i)}
          />
        ))}
      </div> : null}
    </div>
  )
}

/** The newest session-start turn, listed above the chart so its one-off size does
 *  not pin the y-axis. A picker like any chart column: it says what it does, shows the
 *  same size the turn's own headline states, and marks itself when it is the turn shown. */
function StartTurnRow({
  row,
  figure,
  selected,
  onSelect,
}: {
  row: TrendRow
  figure: { n: number; unit: 'chars' | 'tokens' } | null
  selected: boolean
  onSelect: () => void
}) {
  const chars = Object.values(row.ours).reduce((a, b) => a + b, 0) + row.loop
  return (
    <button
      type="button"
      data-start-row
      data-turn={row.turn}
      aria-pressed={selected}
      aria-label={i18nT('pages.contextBreakdown.start_row_pick', { n: fmtN(row.turn) })}
      onClick={onSelect}
      className={`w-full flex items-center justify-between gap-3 px-3 py-2 mb-2 rounded-lg border-2 text-left text-[13px] text-text appearance-none cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] ${
        selected ? 'border-[var(--accent)] bg-[var(--card-hl)]' : 'border-border bg-transparent hover:border-[var(--accent)] hover:bg-[var(--card-hl)]'
      }`}
    >
      <span className="min-w-0">{i18nT('pages.contextBreakdown.start_row', { n: fmtN(row.turn) })}</span>
      <span className="flex items-baseline gap-2 shrink-0">
        <span className="font-mono text-[12px] text-muted tabular-nums" data-testid="start-row-size">
          {figure
            ? `${fmtN(figure.n)} ${figure.unit === 'chars' ? i18nT('pages.contextBreakdown.unit_chars') : i18nT('pages.contextBreakdown.unit_tokens')}`
            : i18nT('pages.contextBreakdown.turn_button_chars', { chars: fmtN(chars) })}
        </span>
        <span
          className={`text-[12px] font-semibold ${selected ? 'text-[var(--accent)]' : 'text-text-strong'}`}
          data-testid="start-row-state"
        >
          {selected ? i18nT('pages.contextBreakdown.start_row_shown') : i18nT('pages.contextBreakdown.start_row_show')}
        </span>
      </span>
    </button>
  )
}

/** The blocks this app sent on the turn, by name, the every-turn members as one. */
function BlockList({ turn }: { turn: LatestTurn }) {
  const [open, setOpen] = useState(false)
  const grouped = Object.entries(groupBlocks(turn.blocks ?? {}))
    .filter(([, chars]) => chars > 0)
    .sort((a, b) => b[1] - a[1])
  const omitted = turn.blocks_omitted ?? 0
  if (grouped.length === 0 && omitted === 0) return null
  // The heading counts every block sent, before the every-turn members are grouped
  // into one list row, and the ones past the list's cap too.
  const sent = Object.values(turn.blocks ?? {}).filter(chars => chars > 0).length + omitted
  // The same disclosure glyph as the Kiro Crew group row: right while shut, down open.
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="mt-3 border-t border-border pt-2" data-testid="turn-blocks">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 bg-transparent border-0 p-0 appearance-none cursor-pointer text-[13px] text-text hover:text-text-strong rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
      >
        <Chevron size={14} className="shrink-0 text-muted" aria-hidden="true" data-testid="blocks-chevron" />
        {i18nT('pages.contextBreakdown.blocks_heading', { count: sent, n: fmtN(sent) })}
      </button>
      {open ? (
        <ul className="m-0 mt-1 p-0 list-none">
          {grouped.map(([label, chars]) => (
            <li key={label} className="flex items-center justify-between gap-3 py-1 text-[12px] text-text" data-block={label}>
              <span className="min-w-0 break-words">{blockName(label)}</span>
              <span className="font-mono text-muted tabular-nums shrink-0">{fmtN(chars)}</span>
            </li>
          ))}
          {omitted > 0 ? (
            // A note, not a control: italic and muted, no pointer.
            <li className="py-1 text-[12px] text-muted italic cursor-default select-text" data-testid="blocks-omitted">
              {i18nT('pages.contextBreakdown.blocks_omitted', { count: omitted, n: fmtN(omitted) })}
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  )
}

/** The pure, data-in view. Kept free of data fetching so the category maths,
 *  the turn window and the selection model are all exercisable from a
 *  fabricated trace. */
export function ContextBreakdownPanel({
  trace,
  isLoading,
  chartWidth,
  harness,
  pastTurn,
  onPastTurn,
}: {
  trace: ContextTrace | null | undefined
  isLoading?: boolean
  /** The session's harness id ('kiro', 'kas', ...), as the selection-capabilities
   *  route reports it; absent while unknown, null when that route failed. */
  harness?: string | null
  /** Fixed chart width in px (capture harnesses and tests); measured when absent. */
  chartWidth?: number
  /** The picked past turn, as read on demand. */
  pastTurn?: PastTurnRead | null
  /** Told which past turn is picked and must be read (null: none). */
  onPastTurn?: (ordinal: number | null) => void
}) {
  let body
  if (isLoading && !trace) {
    body = (
      <div className="text-muted text-[11px] py-6 text-center">
        {i18nT('pages.contextBreakdown.loading')}
      </div>
    )
  } else if (!trace || (!trace.latest && (trace.trend ?? []).length === 0)) {
    // Three roads lead here and only one is final. A session recorded before the fold
    // and one with no completed turn yet each fill on the next turn, so "yet" is true
    // for them. With recording switched off nothing ever arrives, and the payload
    // says so.
    body = (
      <div className="text-muted text-[11px] py-6 text-center" data-testid={trace?.recording === false ? 'context-breakdown-off' : undefined}>
        {trace?.recording === false
          ? i18nT('pages.contextBreakdown.empty_off', { file: trace.env_file || '~/.kiro/crew/.env' })
          : i18nT('pages.contextBreakdown.empty')}
      </div>
    )
  } else {
    // Keyed by the session: a pick belongs to the session it was made in.
    body = <ContextBreakdownCard key={trace.slot} trace={trace} chartWidth={chartWidth} harness={harness} pastTurn={pastTurn} onPastTurn={onPastTurn} />
  }

  return <div>{body}</div>
}

/** The trend chart's bands: this app's five categories, then the rest of the context. */
const TREND_LAYERS: readonly string[] = [...CATEGORIES, 'loop']
// The user's own words take the bar's chat colour, so the two pictures agree.
const TREND_FILL: Readonly<Record<string, string>> = { ...CATEGORY_FILL, message: MESSAGE_COLOR, loop: 'var(--ctx-eng-loop)' }

function ContextBreakdownCard({
  trace,
  chartWidth,
  harness,
  pastTurn,
  onPastTurn,
}: {
  trace: ContextTrace
  chartWidth?: number
  harness?: string | null
  pastTurn?: PastTurnRead | null
  onPastTurn?: (ordinal: number | null) => void
}) {
  const latest = trace.latest ?? null
  const rows = trace.trend ?? []
  const charsPerToken = Math.max(0.1, trace.chars_per_token ?? 4)
  // A harness that never splits its turns heads every panel in tokens; the chart then
  // speaks tokens too, estimated at the composer's ratio, so one panel has one unit.
  const tokenChart = !!latest && !latest.engine && latest.context_used > 0 && rows.every(r => r.loop <= 0)
  const scale = (n: number) => (tokenChart ? Math.round(n / charsPerToken) : n)
  const shown: ChartTurn[] = rows.map((row, i) => {
    const cats: Record<string, number> = Object.fromEntries(
      Object.entries({ ...row.ours, loop: row.loop }).map(([k, v]) => [k, scale(v)]),
    )
    if (tokenChart) {
      // The harness total is the turn's own reading: the grey band fills the column
      // up to it, so each column states the same size its panel heads with.
      const crew = CATEGORIES.reduce((a, c) => a + (cats[c] ?? 0), 0)
      cats.loop = Math.max(0, (row.used ?? 0) - crew)
    }
    return {
      n: i + 1,
      displayN: row.turn,
      total: Object.values(cats).reduce((a, b) => a + b, 0),
      cats,
      isStart: false,
      isFirstStart: false,
      missing: tokenChart && !((row.used ?? 0) > 0),
    }
  })
  // A token column whose turn reported no total is hatched; the caption says so.
  const noTotal = tokenChart && rows.some(r => !((r.used ?? 0) > 0))
  const start = trace.start ?? null
  const firstShown = shown[0]
  // The fold counts exactly how many turns precede the ring; the start row, when it
  // is one of them, is listed above and named as such.
  const lastEarlier = trace.earlier ?? 0
  const startsBefore = start && firstShown && start.turn < firstShown.displayN ? 1 : 0
  const earlierText =
    startsBefore > 0
      ? i18nT('pages.contextBreakdown.earlier_range_starts', { first: fmtN(1), last: fmtN(lastEarlier), count: startsBefore })
      : lastEarlier === 1
        ? i18nT('pages.contextBreakdown.earlier_single', { n: fmtN(1) })
        : i18nT('pages.contextBreakdown.earlier_range', { first: fmtN(1), last: fmtN(lastEarlier) })
  const newestN = shown.length

  // What is picked, by the turn's ORDINAL rather than its column: once the ring is full
  // every closed turn shifts the columns, and a column index would silently move the
  // panel to the next turn. null is the newest turn (`latest`, which may still be
  // running and so not yet on the chart).
  const [picked, setPicked] = useState<{ ordinal: number; start: boolean } | null>(null)
  const pickedStart = picked?.start && start && start.turn === picked.ordinal ? start : null
  const pickedIdx = picked && !picked.start ? rows.findIndex(r => r.turn === picked.ordinal) : -1
  const pickedRow = pickedIdx >= 0 ? rows[pickedIdx] : null
  // The picked turn left the ring (or the start row moved on): show the newest, and say so.
  const gone = picked !== null && !pickedStart && !pickedRow ? picked.ordinal : null
  // The newest turn shows `latest` itself, whichever place it was picked from, so one
  // turn never reads two ways.
  const shownRow = pickedStart ?? pickedRow
  const pastRow = shownRow && shownRow.turn !== latest?.turn ? shownRow : null
  // A past turn's whole panel is read on demand; a fixture row may carry it.
  const fetchOrdinal = pastRow && !pastRow.detail ? pastRow.turn : null
  useEffect(() => {
    onPastTurn?.(fetchOrdinal)
  }, [fetchOrdinal, onPastTurn])
  const read = fetchOrdinal !== null && pastTurn?.ordinal === fetchOrdinal ? pastTurn : null
  const turn: LatestTurn | null = pastRow ? pastRow.detail ?? read?.detail ?? null : latest
  const isNewest = !pastRow
  const newestIdx = newestN > 0 && shown[newestN - 1].displayN === latest?.turn ? newestN : 0
  // The newest turn is not a column yet (it is still running) and not the start row.
  const running = !!latest && newestN > 0 && newestIdx === 0 && !(start && start.turn === latest.turn) && latest.turn > shown[newestN - 1].displayN
  const selectedN = pickedStart ? 0 : pickedRow ? pickedIdx + 1 : start && start.turn === latest?.turn ? 0 : newestIdx
  const startSelected = !!start && (pickedStart !== null || (isNewest && !pickedRow && start.turn === latest?.turn))
  const selectedDisplayN = startSelected && start ? start.turn : selectedN > 0 ? shown[selectedN - 1].displayN : latest?.turn ?? 0

  const breakdownFor = (t: LatestTurn, past: boolean) =>
    breakdownOf(t, [], trace.chars_per_token ?? 4, harness ? acpBackendName({ id: harness }) : '', harness === null ? null : harness ?? '', past)
  const breakdown = turn ? breakdownFor(turn, !isNewest) : null
  // The start row states its turn's headline figure: the newest turn's own when it is
  // the newest, else the one the trace sends with the row.
  const startFigure = !start
    ? null
    : start.turn === latest?.turn && latest
      ? headlineFigure(breakdownFor(latest, false))
      : start.detail
        ? headlineFigure(breakdownFor(start.detail, true))
        : start.figure ?? null
  // One name per turn: a session-start turn is called that wherever it is shown, and
  // "latest" is added when it is also the newest.
  const isStartTurn = !!turn && (turn.phase === 'session_start' || (!!start && start.turn === turn.turn))
  const heading = !turn
    ? ''
    : isStartTurn
      ? isNewest
        ? i18nT('pages.contextBreakdown.turn_start_latest', { n: fmtN(turn.turn) })
        : i18nT('pages.contextBreakdown.start_row', { n: fmtN(turn.turn) })
      : isNewest
        ? i18nT('pages.contextBreakdown.turn_latest', { n: fmtN(turn.turn) })
        : i18nT('pages.contextBreakdown.turn_n', { n: fmtN(turn.turn) })

  return (
    <div className="border border-border bg-card rounded-xl overflow-hidden">
      <div className="px-4 py-4 border-b border-border">
        <h2 className="m-0 text-[17px] font-semibold tracking-tight text-text-strong">
          {i18nT('pages.contextBreakdown.heading')}
        </h2>
        <p className="m-0 mt-1 text-[13px] text-muted">{i18nT('pages.contextBreakdown.subtitle')}</p>
      </div>

      {pastRow ? (
        // The newest turn may still be running and so not be a column yet: this is
        // always the way back to it.
        <div className="mx-4 mt-3 -mb-1 flex justify-end">
          <button
            type="button"
            data-testid="show-latest"
            onClick={() => setPicked(null)}
            className="text-[12px] font-semibold text-[var(--accent)] bg-transparent border border-border rounded-md px-2 py-0.5 cursor-pointer hover:bg-[var(--card-hl)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
          >
            {latest ? i18nT('pages.contextBreakdown.show_latest_n', { n: fmtN(latest.turn) }) : i18nT('pages.contextBreakdown.show_latest')}
          </button>
        </div>
      ) : null}

      {pastRow && !turn ? (
        <div className="mx-4 mt-4" data-testid="past-turn-read">
          <strong className="text-[15px] text-text-strong">
            {pastRow === pickedStart
              ? i18nT('pages.contextBreakdown.start_row', { n: fmtN(pastRow.turn) })
              : i18nT('pages.contextBreakdown.turn_n', { n: fmtN(pastRow.turn) })}
          </strong>
          {read?.error ? (
            <ErrorNotice message={read.error} askAgent className="mt-2" />
          ) : (
            <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.contextBreakdown.loading')}</p>
          )}
        </div>
      ) : null}

      {turn && breakdown ? (
        // Keyed by the turn, so a disclosure opened on one turn starts closed on the next.
        <div className="mx-4 mt-4" data-testid="selected-turn-detail" key={`t${turn.turn}`}>
          {gone !== null ? (
            <p className="m-0 mb-1 text-[12px] text-muted" data-testid="picked-gone">
              {i18nT('pages.contextBreakdown.picked_gone', { n: fmtN(gone) })}
            </p>
          ) : null}
          <strong className="text-[15px] text-text-strong">{heading}</strong>
          <Headline breakdown={breakdown} />
          {shown.length > 0 && !tokenChart && headlineFigure(breakdown)?.unit === 'tokens' ? (
            // An unmeasured turn heads in tokens while the chart below counts characters.
            <p className="m-0 mt-1 text-[12px] text-muted" data-testid="unit-switch">
              {i18nT('pages.contextBreakdown.unit_switch')}
            </p>
          ) : null}
          <TurnBreakdown breakdown={breakdown} engine={turn.engine} ttft={turn.ttft_ms} charsPerToken={trace.chars_per_token} />
          <BlockList turn={turn} />
        </div>
      ) : null}

      <div className="px-4 pt-4 mt-4 border-t border-border" data-testid="trend">
        {start ? (
          <StartTurnRow
            row={start}
            figure={startFigure}
            selected={startSelected}
            onSelect={() => setPicked({ ordinal: start.turn, start: true })}
          />
        ) : null}
        {shown.length > 0 ? (
          <>
            <div className="flex items-center justify-between gap-3 mb-2">
              <span className="text-[13px] font-semibold text-text">
                {lastEarlier > 0
                  ? i18nT('pages.contextBreakdown.scope_turns', { count: shown.length })
                  : i18nT('pages.contextBreakdown.scope_all_turns', { count: shown.length })}
              </span>
              <span className="text-[12px] text-muted" data-testid="chart-unit">
                {tokenChart ? i18nT('pages.contextBreakdown.scope_unit_tokens') : i18nT('pages.contextBreakdown.scope_unit')}
              </span>
            </div>
            {lastEarlier > startsBefore ? (
              <p className="m-0 mb-2 text-[12px] text-muted" data-testid="earlier-turns">
                {earlierText}
              </p>
            ) : null}
            <StackedArea
              turns={shown}
              selected={selectedN}
              selectedDisplayN={selectedDisplayN}
              onSelect={n => setPicked({ ordinal: shown[n - 1].displayN, start: false })}
              width={chartWidth}
              tokens={tokenChart}
              onLatest={() => setPicked(null)}
              layers={TREND_LAYERS}
              fills={TREND_FILL}
            />
            <div className="flex flex-wrap gap-x-4 gap-y-1.5 mt-2 text-[12px] text-muted">
              {TREND_LAYERS.filter(layer => shown.some(t => (t.cats[layer] ?? 0) > 0)).map(layer => (
                <span key={layer} className="flex items-center gap-1.5" data-legend={layer}>
                  <i
                    className="w-2.5 h-2.5 rounded-[2px] shrink-0"
                    style={{ background: TREND_FILL[layer] }}
                    aria-hidden="true"
                  />
                  {layer === 'loop'
                    ? i18nT('pages.contextBreakdown.trend_rest')
                    : i18nT(CATEGORY_KEY[layer as Category])}
                </span>
              ))}
            </div>
            <p className="m-0 mt-2 text-[12px] text-muted">
              {i18nT('pages.contextBreakdown.pick_hint')} {i18nT('pages.contextBreakdown.chart_note')}
              {noTotal ? <span data-testid="chart-no-total"> {i18nT('pages.contextBreakdown.chart_no_total')}</span> : null}
            </p>
            {running ? (
              <p className="m-0 mt-1 text-[12px] text-muted" data-testid="chart-running">
                {i18nT('pages.contextBreakdown.chart_running', { n: fmtN(latest!.turn) })}
              </p>
            ) : null}
          </>
        ) : null}
      </div>

      <div className="h-4" aria-hidden="true" />
    </div>
  )
}

/** A kept row's own version, as the fold stamps it on every write of the row, or
 *  `gone` once no row holds that ordinal (the read then answers 404 and must not be
 *  served stale). An older gateway sends no version; its numbers stand in. */
function rowVersion(trace: ContextTrace | undefined, ordinal: number): string {
  const row = trace?.start?.turn === ordinal ? trace.start : trace?.trend?.find(r => r.turn === ordinal)
  if (!row) return 'gone'
  if (typeof row.version === 'number') return `v${row.version}`
  return JSON.stringify([row.ours, row.loop, row.used ?? null, row.figure ?? null])
}

/** The panel as a per-session side-panel tab.
 *
 *  Scoped to ONE chat slot by construction, which is why there is no session
 *  picker: the tab belongs to the session it was opened from, the same way the
 *  Logs tab does. A global page listing every session was the wrong home for a
 *  per-turn drill-down — it made the reader pick a session before the view could
 *  say anything.
 */
export function ContextBreakdownTab(props: { slot: string; subagents?: Record<string, SubagentActivity> }) {
  // Remounted per session, so a past turn picked in one session never applies to
  // the next one's rows: the new session opens on its newest turn.
  return <ContextBreakdownTabFor key={props.slot} {...props} />
}

function ContextBreakdownTabFor({ slot, subagents }: { slot: string; subagents?: Record<string, SubagentActivity> }) {
  const { data, isLoading, error } = useQuery<ContextTrace>({
    queryKey: contextTraceKey(slot),
    queryFn: () => api.telemetryContextTrace(slot),
    enabled: !!slot,
    // Read on every mount, even over a cached value: the app's default staleTime
    // is Infinity, and frames that landed while the tab was closed refreshed
    // nothing. After that nothing polls: a pushed `usage` frame for this slot
    // asks for one re-read (`hooks/websocket/contextTraceRefresh.ts`), and a
    // reconnect re-reads once.
    refetchOnMount: 'always',
  })
  // The session's harness, from the one route that resolves it (the model picker
  // reads the same query): the live session's backend, else the slot's configured one.
  const caps = useQuery({
    queryKey: ['slot-selection-capabilities', slot],
    queryFn: () => api.chatSlotSelectionCapabilities(slot),
    enabled: !!slot && typeof api.chatSlotSelectionCapabilities === 'function',
  })
  const backend = caps.data?.backend
  // Kiro CLI's runtime id is the empty string; the Agent Harness tab calls it `kiro`.
  // A failed read is null, not unknown-yet: the panel then gives no harness advice.
  const harness = typeof backend === 'string' ? backend || ACP_BACKEND_KIRO : caps.isError ? null : undefined
  const capsError = caps.error ? (caps.error instanceof Error ? caps.error.message : String(caps.error)) : null
  // A picked past turn's whole panel, read from the gateway's in-memory fold. Keyed by
  // the kept row's own numbers rather than the fold revision: a closed row changes only
  // when its turn is sent again, and then its sums or reading change too, so a new turn
  // elsewhere answers from cache. While a changed row is re-read, the previous read of
  // the same ordinal stays on screen, so the panel and its disclosures do not reset.
  const [pastOrdinal, setPastOrdinal] = useState<number | null>(null)
  const pastRowKey = pastOrdinal === null ? null : rowVersion(data, pastOrdinal)
  const past = useQuery<{ turn: LatestTurn }>({
    queryKey: ['context-turn', slot, pastOrdinal, pastRowKey],
    queryFn: () => api.telemetryContextTurn(slot, pastOrdinal as number),
    enabled: !!slot && pastOrdinal !== null && typeof api.telemetryContextTurn === 'function',
    retry: false,
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === slot && previousQuery?.queryKey[2] === pastOrdinal ? previous : undefined,
  })
  const pastTurn: PastTurnRead | null =
    pastOrdinal === null
      ? null
      : {
          ordinal: pastOrdinal,
          detail: past.data?.turn ?? null,
          loading: past.isLoading,
          error: past.error ? (past.error instanceof Error ? past.error.message : String(past.error)) : null,
        }

  return (
    <div className="h-full overflow-auto p-3">
      <SessionBreakdownTree subagents={subagents ?? {}} />
      {/* A failed trace read otherwise rendered as an empty panel. Read-only
          side tab, so the hand-off loses nothing; the next pushed frame re-reads. */}
      <ErrorNotice message={error ? (error instanceof Error ? error.message : String(error)) : null} askAgent className="mb-3" />
      {/* Without the harness the panel cannot say where a split comes from. */}
      <ErrorNotice message={capsError} askAgent className="mb-3" />
      <ContextBreakdownPanel trace={data} isLoading={isLoading} harness={harness} pastTurn={pastTurn} onPastTurn={setPastOrdinal} />
    </div>
  )
}
