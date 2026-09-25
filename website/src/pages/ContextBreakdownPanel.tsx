import { useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { SubagentActivity } from '../types'
import { SessionBreakdownTree } from './SessionBreakdownTree'
import { CATEGORY_FILL } from './contextSourceColors'

/**
 * One turn's injection record, as GET /api/telemetry/context-trace returns it.
 * `context_used` / `context_window` are in TOKENS; every block size is in CHARS.
 * The panel reads only `blocks` and `total_chars`; the other fields stay on the
 * wire for the recorder's other readers.
 */
export interface ContextTurn {
  ts: string
  phase: string
  blocks: Record<string, number>
  total_chars: number
  context_used: number
  context_window: number
  model: string
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
// render time (dynamic, so it needs no catalog entry — the long tail of rare
// blocks never earns a translated string).
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

/** Humanise a block id for the long tail: `hook_context` -> `Hook context`. */
function humanise(label: string): string {
  const spaced = label.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function displayName(label: string): string {
  const key = BLOCK_KEY[label]
  return key ? i18nT(key) : humanise(label)
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

/** Character delta between the selected turn and the one before it. */
function deltaText(current: number, previous: number | undefined): string | null {
  if (previous === undefined) return null
  const diff = current - previous
  if (diff === 0) return i18nT('pages.contextBreakdown.delta_same')
  const n = fmtN(Math.abs(diff))
  return diff > 0
    ? i18nT('pages.contextBreakdown.delta_up', { n })
    : i18nT('pages.contextBreakdown.delta_down', { n })
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
  /** 1-based turn number within the whole trace. */
  n: number
  total: number
  cats: Record<Category, number>
  isStart: boolean
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
  onSelect,
  width: fixedWidth,
}: {
  turns: ChartTurn[]
  selected: number
  onSelect: (n: number) => void
  /** Overrides the measured width (capture harnesses and tests). */
  width?: number
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

  let bottom = valuesOf(() => 0)
  const layers = CATEGORIES.map(cat => {
    const top = bottom.map((v, i) => v + valuesOf(t => t.cats[cat])[i])
    const topPts = top.map((v, i) => `${polyXs[i]},${y(v)}`)
    const bottomPts = bottom.map((v, i) => `${polyXs[i]},${y(v)}`).reverse()
    const layer = { cat, polygon: [...topPts, ...bottomPts].join(' '), line: topPts.join(' ') }
    bottom = top
    return layer
  })
  const totalLine = bottom.map((v, i) => `${polyXs[i]},${y(v)}`).join(' ')

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
        aria-label={i18nT('pages.contextBreakdown.chart_aria', { n: fmtN(selected) })}
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
        {layers.map(layer => (
          <g key={layer.cat} data-category={layer.cat}>
            <polygon points={layer.polygon} fill={CATEGORY_FILL[layer.cat]} fillOpacity={0.83} />
            <polyline points={layer.line} fill="none" stroke="var(--card)" strokeWidth={1} />
          </g>
        ))}
        <polyline points={totalLine} fill="none" stroke="var(--card-fg, var(--text-strong))" strokeWidth={1.5} />
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
              {i18nT('pages.contextBreakdown.axis_turn_n', { n: fmtN(t.n) })}
            </text>
          ) : null,
        )}
      </svg>
      {/* Transparent hit columns: one real button per turn so selection is
          clickable, focusable and arrow-key navigable. */}
      <div className="absolute inset-y-0 left-0 right-0 cursor-pointer" role="group" aria-label={i18nT('pages.contextBreakdown.turn_picker')}>
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
            aria-label={i18nT('pages.contextBreakdown.turn_button', { n: fmtN(t.n), chars: fmtN(t.total) })}
            data-turn={t.n}
            className={`absolute inset-y-0 appearance-none bg-transparent border-0 p-0 m-0 cursor-pointer rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] ${
              pointerSurface ? 'pointer-events-none' : 'hover:bg-[var(--card-hl)]'
            }`}
            style={{ left: hitLeft(i), width: hitWidth }}
            onClick={() => onSelect(t.n)}
            onKeyDown={e => onArrow(e, i)}
          />
        ))}
      </div>
    </div>
  )
}

/** A session-start turn, listed above the chart so its size does not pin the
 *  y-axis and flatten every later turn. Selectable like any chart turn. */
function StartTurnRow({ turn, selected, onSelect }: { turn: ChartTurn; selected: boolean; onSelect: (n: number) => void }) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      data-turn={turn.n}
      data-start-row
      className={`w-full flex items-center justify-between gap-3 px-3 py-2 mb-2 rounded-lg border text-left text-[13px] cursor-pointer appearance-none bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)] ${
        selected ? 'border-[var(--accent)] text-text-strong bg-[var(--bg-accent)]' : 'border-border text-text hover:bg-[var(--card-hl)]'
      }`}
      onClick={() => onSelect(turn.n)}
    >
      <span>{i18nT('pages.contextBreakdown.start_row', { n: fmtN(turn.n) })}</span>
      <span className="font-mono text-[12px] text-muted tabular-nums shrink-0">
        {i18nT('pages.contextBreakdown.turn_button_chars', { chars: fmtN(turn.total) })}
      </span>
    </button>
  )
}

/** One category of the selected turn: colour dot, name, count, and the raw
 *  blocks behind it as a disclosure. */
function CategoryRow({ cat, chars, blocks }: { cat: Category; chars: number; blocks: Record<string, number> }) {
  const [open, setOpen] = useState(false)
  const name = i18nT(CATEGORY_KEY[cat])
  const parts = Object.entries(groupBlocks(blocks)).sort((a, b) => b[1] - a[1])
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="border-b border-border last:border-b-0" data-category-row={cat}>
      <button
        type="button"
        className="w-full flex items-center justify-between gap-3 py-2.5 text-left bg-transparent border-0 appearance-none cursor-pointer text-[13px] text-text hover:text-text-strong rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
      >
        <span className="flex items-center gap-2 min-w-0">
          <i className="w-2.5 h-2.5 rounded-[2px] shrink-0" style={{ background: CATEGORY_FILL[cat] }} aria-hidden="true" />
          <span className="truncate">{name}</span>
          <Chevron size={14} className="lucide-inline shrink-0 text-muted" aria-hidden="true" />
        </span>
        <span className="font-mono text-[12px] text-muted tabular-nums shrink-0">{fmtN(chars)}</span>
      </button>
      {open ? (
        <ul className="list-none m-0 mb-2 ml-1 pl-4 border-l-2 border-border">
          {parts.map(([label, n]) => (
            <li key={label} className="flex items-center justify-between gap-3 py-1 text-[12px] text-text">
              <span className="truncate">{displayName(label)}</span>
              <span className="font-mono text-muted tabular-nums shrink-0">{fmtN(n)}</span>
            </li>
          ))}
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
}: {
  trace: ContextTrace | null | undefined
  isLoading?: boolean
  /** Fixed chart width in px (capture harnesses and tests); measured when absent. */
  chartWidth?: number
}) {
  let body
  if (isLoading && !trace) {
    body = (
      <div className="text-muted text-[11px] py-6 text-center">
        {i18nT('pages.contextBreakdown.loading')}
      </div>
    )
  } else if (!trace || trace.turns.length === 0) {
    body = (
      <div className="text-muted text-[11px] py-6 text-center">
        {i18nT('pages.contextBreakdown.empty')}
      </div>
    )
  } else {
    body = <ContextBreakdownCard trace={trace} chartWidth={chartWidth} />
  }

  return <div>{body}</div>
}

function ContextBreakdownCard({ trace, chartWidth }: { trace: ContextTrace; chartWidth?: number }) {
  // `null` follows the newest turn as the trace grows; a number pins a turn the
  // user chose, so a new row arriving does not yank the detail view away.
  const [pinned, setPinned] = useState<number | null>(null)

  const all: ChartTurn[] = trace.turns.map((turn, i) => ({
    n: i + 1,
    total: turn.total_chars,
    cats: categorise(turn.blocks),
    isStart: turn.phase === 'session_start',
  }))
  // Session-start turns are listed above the chart: one of them is many times
  // the size of any later turn and would pin the y-axis, flattening the rest.
  const starts = all.filter(t => t.isStart)
  const regular = all.filter(t => !t.isStart)
  const hidden = Math.max(0, regular.length - MAX_CHART_TURNS)
  const shown = regular.slice(hidden)
  const newest = all.length
  const selectable = new Set([...starts, ...shown].map(t => t.n))
  const selected = pinned !== null && selectable.has(pinned) ? pinned : newest
  const selectedTurn = trace.turns[selected - 1]
  const selectedChart = all[selected - 1]
  const previous = selected > 1 ? all[selected - 2].total : undefined
  const delta = deltaText(selectedChart.total, previous)
  const select = (n: number) => setPinned(n === newest ? null : n)

  const rows = CATEGORIES.map(cat => ({
    cat,
    chars: selectedChart.cats[cat],
    blocks: Object.fromEntries(Object.entries(selectedTurn.blocks).filter(([label]) => categoryOf(label) === cat)),
  })).filter(r => r.chars > 0)

  return (
    <div className="border border-border bg-card rounded-xl overflow-hidden">
      <div className="px-4 py-4 border-b border-border">
        <h2 className="m-0 text-[17px] font-semibold tracking-tight text-text-strong">
          {i18nT('pages.contextBreakdown.heading')}
        </h2>
        <p className="m-0 mt-1 text-[13px] text-muted">{i18nT('pages.contextBreakdown.subtitle')}</p>
      </div>

      <div className="px-4 pt-4">
        {starts.map(t => (
          <StartTurnRow key={t.n} turn={t} selected={t.n === selected} onSelect={select} />
        ))}

        {shown.length > 0 ? (
          <>
            <div className="flex items-center justify-between gap-3 mb-2">
              <span className="text-[13px] font-semibold text-text">
                {hidden > 0
                  ? i18nT('pages.contextBreakdown.scope_turns', { count: shown.length })
                  : i18nT('pages.contextBreakdown.scope_all_turns', { count: shown.length })}
              </span>
              <span className="text-[12px] text-muted">{i18nT('pages.contextBreakdown.scope_unit')}</span>
            </div>
            {hidden > 0 ? (
              <p className="m-0 mb-2 text-[12px] text-muted">
                {i18nT('pages.contextBreakdown.earlier_hidden', { count: hidden })}
              </p>
            ) : null}

            <StackedArea turns={shown} selected={selected} onSelect={select} width={chartWidth} />

            <div className="flex flex-wrap gap-x-4 gap-y-1.5 mt-2 text-[12px] text-muted">
              {CATEGORIES.map(cat => (
                <span key={cat} className="flex items-center gap-1.5">
                  <i className="w-2.5 h-2.5 rounded-[2px] shrink-0" style={{ background: CATEGORY_FILL[cat] }} aria-hidden="true" />
                  {i18nT(CATEGORY_KEY[cat])}
                </span>
              ))}
            </div>
            <p className="m-0 mt-2 text-[12px] text-muted">{i18nT('pages.contextBreakdown.pick_hint')}</p>
            <p className="m-0 mt-2 text-[12px] text-muted">
              {i18nT('pages.contextBreakdown.chart_note')}
              {starts.length > 0 ? <> {i18nT('pages.contextBreakdown.start_note')}</> : null}
            </p>
          </>
        ) : null}
      </div>

      <div className="mx-4 mt-4 pt-4 border-t border-border" data-testid="selected-turn-detail">
        <div className="flex items-center justify-between gap-3">
          <strong className="text-[15px] text-text-strong">
            {selected === newest
              ? i18nT('pages.contextBreakdown.turn_latest', { n: fmtN(selected) })
              : i18nT('pages.contextBreakdown.turn_n', { n: fmtN(selected) })}
          </strong>
          {delta ? <span className="text-[12px] text-muted">{delta}</span> : null}
        </div>
        <div className="mt-1 mb-3 text-[28px] font-semibold tracking-tight tabular-nums text-text-strong">
          {fmtN(selectedChart.total)}
          <span className="ml-1.5 text-[13px] font-normal tracking-normal text-muted">
            {i18nT('pages.contextBreakdown.unit_chars')}
          </span>
        </div>
        {rows.map(r => (
          <CategoryRow key={r.cat} cat={r.cat} chars={r.chars} blocks={r.blocks} />
        ))}
      </div>

      <p className="m-0 mx-4 mt-4 mb-4 pt-3 border-t border-border text-[12px] text-muted">
        {i18nT('pages.contextBreakdown.footer')}
      </p>
    </div>
  )
}

/** The panel as a per-session side-panel tab.
 *
 *  Scoped to ONE chat slot by construction, which is why there is no session
 *  picker: the tab belongs to the session it was opened from, the same way the
 *  Logs tab does. A global page listing every session was the wrong home for a
 *  per-turn drill-down — it made the reader pick a session before the view could
 *  say anything.
 */
export function ContextBreakdownTab({ slot, subagents }: { slot: string; subagents?: Record<string, SubagentActivity> }) {
  const { data, isLoading, error } = useQuery<ContextTrace>({
    queryKey: ['context-trace', slot],
    queryFn: () => api.telemetryContextTrace(slot),
    enabled: !!slot,
    // The trace grows by one row per turn, so a tab left open goes stale.
    refetchInterval: 15_000,
  })

  return (
    <div className="h-full overflow-auto p-3">
      <SessionBreakdownTree subagents={subagents ?? {}} />
      {/* A failed trace read otherwise rendered as an empty panel. Read-only
          side tab, so the hand-off loses nothing; the poll above retries. */}
      <ErrorNotice message={error ? (error instanceof Error ? error.message : String(error)) : null} askAgent className="mb-3" />
      <ContextBreakdownPanel trace={data} isLoading={isLoading} />
    </div>
  )
}
