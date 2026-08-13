import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { fmtNumber, fmtPercent } from '../i18n/format'
import { i18nT } from '../i18n/t'

/**
 * One turn's injection record, as GET /api/telemetry/context-trace returns it.
 * `context_used` / `context_window` are in TOKENS; every block size is in CHARS,
 * so the two are only comparable through the backend's estimate (see below).
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
  // The model context KiroCrew did NOT inject (kiro-cli's own prompt + tool
  // catalogue), derived from peak token occupancy via a chars-per-token ratio.
  // An ESTIMATE — surfaced hatched and labelled as one everywhere it renders.
  estimated_other_chars: number
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
  skill_index: 'pages.contextBreakdown.block_skill_index',
  skill_hint: 'pages.contextBreakdown.block_skill_hint',
  loaded_skill: 'pages.contextBreakdown.block_loaded_skill',
  critical_rules: 'pages.contextBreakdown.block_critical_rules',
  [EVERY_TURN_KEY]: 'pages.contextBreakdown.block_every_turn',
  unclassified: 'pages.contextBreakdown.block_unclassified',
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

/**
 * Bar length as a fraction of the widest turn, on a SQUARE-ROOT scale.
 *
 * A session's turns span ~70x (a session-start turn dwarfs a follow-up), and on
 * a linear scale the small turns collapse to unreadable stubs. sqrt compresses
 * that spread — the biggest turn still dominates, but a 1.5k-char turn stays
 * wide enough to read its own composition, which is the whole point of the row.
 */
export function barWidthPct(total: number, maxTotal: number): number {
  if (maxTotal <= 0 || total <= 0) return 0
  return Math.sqrt(total / maxTotal) * 100
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

/**
 * Maps a block's size rank onto the eight-step `--ctx-k*` ramp defined in
 * index.css, which is mixed from the theme's own foreground/surface tokens so it
 * tracks every theme and its polarity. Rank 0 (the largest block) takes the most
 * prominent step. `fg` is the ramp's contrasting endpoint (not another mid-mix,
 * which would leave the label nearly invisible on its own fill).
 */
export function rampShade(rank: number, count: number): { fill: string; fg: string } {
  const steps = 8
  const span = count > 1 ? Math.min(Math.max(rank, 0), count - 1) / (count - 1) : 0
  const step = Math.min(steps, Math.max(1, Math.round(1 + span * (steps - 1))))
  return { fill: `var(--ctx-k${step})`, fg: `var(--ctx-fg${step})` }
}

/** Stable colour per label, ranked by WHOLE-SESSION size so a block keeps one
 *  shade across every row. */
function buildColorMap(totals: Record<string, number>): Map<string, { fill: string; fg: string }> {
  const grouped = groupBlocks(totals)
  const ranked = Object.entries(grouped)
    .filter(([label]) => label !== USER_LABEL)
    .sort((a, b) => b[1] - a[1])
    .map(([label]) => label)
  const map = new Map<string, { fill: string; fg: string }>()
  ranked.forEach((label, i) => map.set(label, rampShade(i, ranked.length)))
  return map
}

// Both go through the app-language formatters rather than the host locale, so
// digits and separators match the translated UI around them. Percent values are
// carried as a ratio because fmtPercent renders the unit itself.
const fmtN = (n: number): string => fmtNumber(Math.round(n))
const fmtPct = (p: number): string =>
  fmtPercent(p / 100, { maximumFractionDigits: Number.isInteger(Math.round(p * 10) / 10) ? 0 : 1 })

const USER_SEG = { fill: 'var(--accent)', fg: 'var(--ctx-user-fg)' }
const ESTIMATE_FILL = 'var(--ctx-hatch)'

interface Seg {
  key: string
  label: string
  pct: number
  fill: string
  fg: string
  // Native-tooltip text shown on hover. Bars carry NO baked-in labels — colour
  // is decoded by the legend below, and hovering a segment reveals what it is
  // and how much of the turn it took.
  title: string
  isUser?: boolean
  isEstimate?: boolean
}

function turnSegments(
  blocks: Record<string, number>,
  total: number,
  colorOf: (label: string) => { fill: string; fg: string },
): Seg[] {
  const grouped = groupBlocks(blocks)
  const nonUser = Object.entries(grouped)
    .filter(([label]) => label !== USER_LABEL)
    .sort((a, b) => b[1] - a[1])
  const segs: Seg[] = nonUser.map(([label, chars]) => {
    const name = displayName(label)
    const pct = total > 0 ? (chars / total) * 100 : 0
    return {
      key: label,
      label: name,
      pct,
      title: i18nT('pages.contextBreakdown.segment_label', { label: name, pct: fmtPct(pct) }),
      ...colorOf(label),
    }
  })
  const userChars = grouped[USER_LABEL] ?? 0
  if (userChars > 0) {
    const name = displayName(USER_LABEL)
    const pct = total > 0 ? (userChars / total) * 100 : 0
    segs.push({
      key: USER_LABEL,
      label: name,
      pct,
      title: i18nT('pages.contextBreakdown.segment_label', { label: name, pct: fmtPct(pct) }),
      isUser: true,
      ...USER_SEG,
    })
  }
  return segs
}

/** The bar: pure coloured proportion, no baked-in text. Colour is decoded by
 *  the legend below; hovering a segment pops a small styled bubble naming it and
 *  its share. The bubble is a real DOM element (not the native `title`, which is
 *  OS-drawn and never shows in a screen recording) and is `position: fixed` so
 *  it escapes the row's `overflow-hidden` clip. The human slice keeps a small
 *  min-width so a few-character message stays a visible, hoverable sliver. */
function Bar({ segs, widthPct }: { segs: Seg[]; widthPct: number }) {
  const [tip, setTip] = useState<{ text: string; x: number; y: number } | null>(null)
  return (
    <>
      <div
        className="absolute left-0 top-0 h-full flex rounded-[3px] overflow-hidden"
        style={{ width: `${widthPct}%` }}
      >
        {segs.map(seg => (
          <div
            key={seg.key}
            className="h-full min-w-0 cursor-default"
            data-user={seg.isUser ? 'true' : undefined}
            data-estimate={seg.isEstimate ? 'true' : undefined}
            data-tip={seg.title}
            style={{
              width: `${seg.pct}%`,
              background: seg.fill,
              ...(seg.isUser ? { minWidth: '3px' } : {}),
            }}
            onMouseEnter={e => setTip({ text: seg.title, x: e.clientX, y: e.clientY })}
            onMouseMove={e => setTip({ text: seg.title, x: e.clientX, y: e.clientY })}
            onMouseLeave={() => setTip(null)}
          />
        ))}
      </div>
      {tip ? (
        <div
          role="tooltip"
          className="fixed z-50 pointer-events-none px-2 py-1 rounded-md text-[11px] font-mono whitespace-nowrap shadow-md"
          style={{
            left: tip.x + 12,
            top: tip.y - 30,
            background: 'var(--bg-elevated)',
            color: 'var(--text)',
            border: '1px solid var(--border)',
          }}
        >
          {tip.text}
        </div>
      ) : null}
    </>
  )
}

/** One per-turn row: number · pure-colour bar · absolute chars. The bar carries
 *  no text; the legend decodes colour and hover reveals each segment. */
function TurnRow({
  n,
  turn,
  maxTotal,
  colorOf,
}: {
  n: number
  turn: ContextTurn
  maxTotal: number
  colorOf: (label: string) => { fill: string; fg: string }
}) {
  const isStart = turn.phase === 'session_start'
  const total = turn.total_chars
  const width = barWidthPct(total, maxTotal)
  const segs = turnSegments(turn.blocks, total, colorOf)

  return (
    <div className="grid grid-cols-[3.5rem_1fr_5rem] gap-2.5 items-center px-3.5 py-[3px] hover:bg-[var(--bg-hover)] rounded">
      <div className="font-mono text-[11px] text-muted text-right whitespace-nowrap">
        <b className="text-text font-medium">{n}</b>
        {isStart ? <> {i18nT('pages.contextBreakdown.row_start')}</> : null}
      </div>
      <div className="relative h-5 overflow-hidden">
        <Bar segs={segs} widthPct={width} />
      </div>
      <div className="font-mono text-[11px] text-text text-right tabular-nums">{fmtN(total)}</div>
    </div>
  )
}

function Stat({ label, value, sub, accent }: { label: string; value: string; sub: string; accent?: boolean }) {
  return (
    <div className="px-3.5 py-3 border-r border-border last:border-r-0">
      <div className="font-mono text-[10px] text-muted uppercase tracking-wide mb-0.5">{label}</div>
      <div
        className="font-mono text-[19px] leading-tight tabular-nums"
        style={{ color: accent ? 'var(--accent)' : 'var(--text-strong)' }}
      >
        {value}
      </div>
      <div className="font-mono text-[10px] text-muted-strong mt-0.5">{sub}</div>
    </div>
  )
}

/** The pure, data-in view. Kept free of data fetching so the maths, grouping,
 *  min-width and estimate handling are all exercisable from a fabricated trace. */
export function ContextBreakdownPanel({
  trace,
  isLoading,
}: {
  trace: ContextTrace | null | undefined
  isLoading?: boolean
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
    body = <ContextBreakdownCard trace={trace} />
  }

  return <div>{body}</div>
}

function ContextBreakdownCard({ trace }: { trace: ContextTrace }) {
  const colorMap = buildColorMap(trace.totals)
  const darkest = rampShade(1, 1)
  const colorOf = (label: string) => colorMap.get(label) ?? darkest

  const totalWindow = trace.injected_chars + trace.estimated_other_chars
  const kirocrewAdded = Math.max(0, trace.injected_chars - trace.user_chars)
  const pctOf = (n: number) => (totalWindow > 0 ? (n / totalWindow) * 100 : 0)

  const numbered = trace.turns.map((turn, i) => ({ turn, n: i + 1 }))
  const starts = numbered.filter(t => t.turn.phase === 'session_start')
  const perTurn = numbered.filter(t => t.turn.phase !== 'session_start')
  const maxTotal = Math.max(1, ...trace.turns.map(t => t.total_chars))

  // Whole-window summary: aggregate blocks + the estimated non-KiroCrew remainder.
  const groupedTotals = groupBlocks(trace.totals)
  const windowNonUser = Object.entries(groupedTotals)
    .filter(([label]) => label !== USER_LABEL)
    .sort((a, b) => b[1] - a[1])
  const windowSegs: Seg[] = []
  if (trace.estimated_other_chars > 0) {
    windowSegs.push({
      key: '__estimate__',
      label: i18nT('pages.contextBreakdown.block_kiro_builtin'),
      pct: pctOf(trace.estimated_other_chars),
      title: i18nT('pages.contextBreakdown.estimate_label', { pct: fmtPct(pctOf(trace.estimated_other_chars)) }),
      fill: ESTIMATE_FILL,
      fg: 'var(--muted)',
      isEstimate: true,
    })
  }
  for (const [label, chars] of windowNonUser) {
    const name = displayName(label)
    const pct = pctOf(chars)
    windowSegs.push({
      key: label,
      label: name,
      pct,
      title: i18nT('pages.contextBreakdown.segment_label', { label: name, pct: fmtPct(pct) }),
      ...colorOf(label),
    })
  }
  if (trace.user_chars > 0) {
    const name = displayName(USER_LABEL)
    const pct = pctOf(trace.user_chars)
    windowSegs.push({
      key: USER_LABEL,
      label: name,
      pct,
      title: i18nT('pages.contextBreakdown.segment_label', { label: name, pct: fmtPct(pct) }),
      isUser: true,
      ...USER_SEG,
    })
  }

  const legend: { key: string; label: string; chars: number; fill: string; estimate?: boolean }[] = [
    { key: USER_LABEL, label: displayName(USER_LABEL), chars: trace.user_chars, fill: USER_SEG.fill },
    ...windowNonUser.map(([label, chars]) => ({ key: label, label: displayName(label), chars, fill: colorOf(label).fill })),
  ]
  if (trace.estimated_other_chars > 0) {
    legend.push({
      key: '__estimate__',
      label: i18nT('pages.contextBreakdown.block_kiro_builtin'),
      chars: trace.estimated_other_chars,
      fill: ESTIMATE_FILL,
      estimate: true,
    })
  }

  return (
    <div className="border border-border bg-card rounded-xl overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-3.5 py-3 border-b border-border bg-[var(--bg-accent)]">
        <span className="text-[11.5px] font-semibold uppercase tracking-wide text-text">
          {i18nT('pages.contextBreakdown.title')}
        </span>
        <span className="font-mono text-[11px] text-muted">
          {i18nT('pages.contextBreakdown.card_meta', {
            turns: fmtN(trace.turns.length),
            chars: fmtN(trace.injected_chars),
          })}
        </span>
      </div>

      <div className="grid grid-cols-3 border-b border-border">
        <Stat
          label={i18nT('pages.contextBreakdown.strip_your_messages')}
          value={fmtPct(pctOf(trace.user_chars))}
          sub={i18nT('pages.contextBreakdown.strip_your_messages_sub', {
            chars: fmtN(trace.user_chars),
            total: fmtN(totalWindow),
          })}
          accent
        />
        <Stat
          label={i18nT('pages.contextBreakdown.strip_kirocrew_added')}
          value={fmtPct(pctOf(kirocrewAdded))}
          sub={i18nT('pages.contextBreakdown.strip_kirocrew_added_sub', { chars: fmtN(kirocrewAdded) })}
        />
        <Stat
          label={i18nT('pages.contextBreakdown.strip_kiro_builtin')}
          value={fmtPct(pctOf(trace.estimated_other_chars))}
          sub={i18nT('pages.contextBreakdown.strip_kiro_builtin_sub', {
            chars: fmtN(trace.estimated_other_chars),
          })}
        />
      </div>

      <div className="grid grid-cols-[3.5rem_1fr_5rem] gap-2.5 px-3.5 pt-2.5 pb-1 font-mono text-[10px] text-muted-strong tracking-wide">
        <span>{i18nT('pages.contextBreakdown.axis_turn')}</span>
        <span>{i18nT('pages.contextBreakdown.axis_bar')}</span>
        <span className="text-right">{i18nT('pages.contextBreakdown.axis_chars')}</span>
      </div>

      {starts.map(({ turn, n }) => (
        <TurnRow key={n} n={n} turn={turn} maxTotal={maxTotal} colorOf={colorOf} />
      ))}

      {perTurn.length > 0 ? (
        <div className="flex items-center gap-2.5 px-3.5 pt-2.5 pb-1">
          <span className="font-mono text-[10px] text-muted-strong uppercase tracking-wide">
            {i18nT('pages.contextBreakdown.group_per_turn')}
          </span>
          <span className="flex-1 h-px bg-border" />
        </div>
      ) : null}

      {perTurn.map(({ turn, n }) => (
        <TurnRow key={n} n={n} turn={turn} maxTotal={maxTotal} colorOf={colorOf} />
      ))}

      <div className="px-3.5 pt-3 pb-1 border-t border-border mt-1.5">
        <div className="font-mono text-[10px] text-muted-strong uppercase tracking-wide pb-1">
          {i18nT('pages.contextBreakdown.group_whole_window')}
        </div>
        <div className="grid grid-cols-[3.5rem_1fr_5rem] gap-2.5 items-center py-[3px]">
          <div className="font-mono text-[11px] text-muted text-right">
            <b className="text-text font-medium">{i18nT('pages.contextBreakdown.row_all')}</b>
          </div>
          <div className="relative h-5">
            <Bar segs={windowSegs} widthPct={100} />
          </div>
          <div className="font-mono text-[11px] text-text text-right tabular-nums">{fmtN(totalWindow)}</div>
        </div>
      </div>

      <div className="flex flex-wrap gap-x-5 gap-y-1 px-3.5 py-3 border-t border-border bg-[var(--bg-accent)]">
        {legend.map(item => (
          <span key={item.key} className="flex items-center gap-1.5 text-[11px] min-w-[13rem]">
            <i
              className="w-2 h-2 rounded-[2px] shrink-0"
              style={{ background: item.fill }}
              aria-hidden="true"
            />
            <span className="text-text">{item.label}</span>
            {item.estimate ? (
              <span className="font-mono text-[10px] text-muted border border-dashed border-border-strong rounded px-1">
                {i18nT('pages.contextBreakdown.tag_estimate')}
              </span>
            ) : null}
            <span className="ml-auto font-mono text-[10.5px] text-muted tabular-nums">{fmtN(item.chars)}</span>
          </span>
        ))}
      </div>

      <div className="px-3.5 pb-3 text-[11px] text-muted">{i18nT('pages.contextBreakdown.caption')}</div>
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
export function ContextBreakdownTab({ slot }: { slot: string }) {
  const { data, isLoading } = useQuery<ContextTrace>({
    queryKey: ['context-trace', slot],
    queryFn: () => api.telemetryContextTrace(slot),
    enabled: !!slot,
    // The trace grows by one row per turn, so a tab left open goes stale.
    refetchInterval: 15_000,
  })

  return (
    <div className="h-full overflow-auto p-3">
      <ContextBreakdownPanel trace={data} isLoading={isLoading} />
    </div>
  )
}
