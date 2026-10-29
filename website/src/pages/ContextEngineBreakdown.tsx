import { useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import InfoTip from '../components/InfoTip'

import { fmtBytes, fmtCompact, fmtList, fmtNumber, fmtPercent, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'

/**
 * What the engine measured of the newest turn, as GET /api/telemetry/context-trace
 * returns it on `latest.engine`. Every size is in CHARACTERS and describes the turn's
 * LAST model request; only `window` is in tokens.
 */
export interface EngineMeasurement {
  total_chars: number | null
  model_calls: number | null
  /** Engine category -> characters, for the categories this version knows. */
  categories: Record<string, number>
  /** The engine's own share of each category, in percent with one decimal. */
  percent?: Record<string, number>
  /** Entries in the agents, skills and powers lists. */
  counts?: Record<string, number>
  /** Category -> its own split (toolIO input/output, workspace and memory parts). */
  parts?: Record<string, Record<string, number>>
  /** The model's input window and how full the backend said it was. */
  window?: { size_tokens: number | null; usage_percent: number | null } | null
  /** Images and documents the request carried, OUTSIDE `total_chars`. */
  media?: { images?: number; image_bytes?: number; documents?: number; document_bytes?: number } | null
  /** The history category's own split: user, assistant, thinking, compaction_summary. */
  history: Record<string, number>
  /** Characters in categories this version does not know, kept by name on the trace. */
  unknown_chars: number
  compacted: boolean
  schema_known: boolean
  /** Characters of every prompt this app composed that is still in the engine's context. */
  crew_chars: number
  /** `crew_chars` split into the panel's five categories. */
  crew_split?: Partial<Record<CrewCategory, number>> | null
  /** User-role text in the engine's context that this app did not compose. */
  user_other_chars: number | null
  /** How far this app's own count exceeds the engine's user text. Zero when they agree. */
  mismatch_chars: number
  /** A turn was sent again since the sums last restarted, so this app's share may
   *  count an attempt the engine dropped. */
  approximate?: boolean
}

/** The turn's wait for its first output, in milliseconds. */
export interface EngineTtft {
  total_ms: number
  setup_ms?: number
  model_ms?: number
}

/** The panel's own five categories (mirrors `Category` in ContextBreakdownPanel). */
export type CrewCategory = 'message' | 'memory' | 'rules' | 'skills' | 'other'

/** One turn whole: the newest on `ContextTrace.latest`, or any kept turn's `detail`. */
export interface LatestTurn {
  /** The turn's number in the whole session. */
  turn: number
  phase: string
  /** This app's five running sums since the model's context last restarted, already
   *  held to what the engine measured when it measured the turn. */
  ours: Record<CrewCategory, number>
  loop_chars: number
  loop_estimated: boolean
  engine: EngineMeasurement | null
  ttft_ms: EngineTtft | null
  /** The occupancy reading, in TOKENS: the newest one, or a past turn's own. */
  context_used: number
  context_window: number
  /** This app's blocks the turn sent, by name, in characters. */
  blocks?: Record<string, number>
  /** Blocks past the per-turn cap, counted but not named. */
  blocks_omitted?: number
}

const fmtN = (n: number): string => fmtNumber(Math.round(n))
const fmtSeconds = (ms: number): string => fmtUnit(ms / 1000, 'second', { maximumFractionDigits: 1 })

// A part too small to round to 0.1% still reads as present, not as zero.
const fmtShare = (share: number): string =>
  share > 0 && share < 0.001
    ? `<${fmtPercent(0.001, { maximumFractionDigits: 1 })}`
    : fmtPercent(share, { maximumFractionDigits: share < 0.01 ? 1 : 0 })

/** `1000000` -> `1M`, `200000` -> `200K`: the window as people say it, in the locale's own short form. */
const fmtTokensShort = (n: number): string => fmtCompact(n)

// --------------------------------------------------------------------------- //
// The tree
// --------------------------------------------------------------------------- //


/** The user's own words, in the chart and on the bar. */
export const MESSAGE_COLOR = 'var(--ctx-your-message)'
const CREW_COLOR = 'var(--ctx-cat-rules)'
const UNSPLIT_COLOR = 'var(--ctx-eng-loop)'

const CREW_PARTS: { cat: Exclude<CrewCategory, 'message'>; labelKey: string }[] = [
  { cat: 'rules', labelKey: 'pages.contextBreakdown.cat_rules' },
  { cat: 'memory', labelKey: 'pages.contextBreakdown.cat_memory' },
  { cat: 'skills', labelKey: 'pages.contextBreakdown.cat_skills' },
  { cat: 'other', labelKey: 'pages.contextBreakdown.cat_other' },
]

/** The engine's categories after Messages, with their catalog keys. */
const CATEGORY_KEY: Record<string, string> = {
  toolSpecs: 'pages.contextBreakdown.engine_cat_toolSpecs',
  toolIO: 'pages.contextBreakdown.engine_cat_toolIO',
  mcpTools: 'pages.contextBreakdown.engine_cat_mcpTools',
  steering: 'pages.contextBreakdown.engine_cat_steering',
  kiroInstructions: 'pages.contextBreakdown.engine_cat_kiroInstructions',
  agentsIndex: 'pages.contextBreakdown.engine_cat_agentsIndex',
  skillsIndex: 'pages.contextBreakdown.engine_cat_skillsIndex',
  powersIndex: 'pages.contextBreakdown.engine_cat_powersIndex',
  workspace: 'pages.contextBreakdown.engine_cat_workspace',
  memory: 'pages.contextBreakdown.engine_cat_memory',
  skills: 'pages.contextBreakdown.engine_cat_skills',
  attachedFiles: 'pages.contextBreakdown.engine_cat_attachedFiles',
  other: 'pages.contextBreakdown.engine_cat_other',
}

// A category's own split, part -> catalog key, in the engine's order.
const PART_KEY: Record<string, [string, string][]> = {
  toolIO: [
    ['input', 'pages.contextBreakdown.part_toolIO_input'],
    ['output', 'pages.contextBreakdown.part_toolIO_output'],
  ],
  workspace: [
    ['file_tree', 'pages.contextBreakdown.part_workspace_file_tree'],
    ['open_files', 'pages.contextBreakdown.part_workspace_open_files'],
    ['environment', 'pages.contextBreakdown.part_workspace_environment'],
    ['repositories', 'pages.contextBreakdown.part_workspace_repositories'],
  ],
  memory: [
    ['learnings', 'pages.contextBreakdown.part_memory_learnings'],
    ['knowledge', 'pages.contextBreakdown.part_memory_knowledge'],
  ],
}

const HISTORY_TAIL: [string, string, string][] = [
  ['assistant', 'pages.contextBreakdown.engine_hist_assistant', 'var(--ctx-eng-history-assistant)'],
  ['thinking', 'pages.contextBreakdown.engine_hist_thinking', 'var(--ctx-eng-history-thinking)'],
  ['compaction_summary', 'pages.contextBreakdown.engine_hist_summary', 'var(--ctx-eng-history-summary)'],
]

/** One row of the list, and (for a leaf) one segment of the bar. */
export interface Row {
  id: string
  label: string
  /** Characters; null when this turn did not report the row's size. */
  chars: number | null
  /** Share of the request's characters, 0..1: the bar width and the percent column. */
  share: number
  color: string
  /** Entries in a list category, shown as "(N)". */
  count?: number
  children?: Row[]
  /** A plain sub-line (a category's own split): number only, no swatch, no share. */
  plain?: boolean
  /** Its children can be hidden with a click. */
  collapsible?: boolean
  /** The last part of a group on the bar: a small gap follows it. */
  groupEnd?: boolean
  /** Tokens, when the row is told in tokens (a turn that was not split). */
  tokens?: number
  /** The tokens are an estimate, written with a leading "~". */
  estimated?: boolean
  /** Help shown from an info glyph after the label. */
  tip?: string
  /** A short visible label beside the tip, so its kind reads before any hover. */
  tipTag?: string
}

/**
 * The newest turn as one tree. `measured`: every part the engine counted, shares
 * are the engine's own percent of the request's characters. `crew`: only Kiro
 * Crew's blocks and the user's own messages have a size; the rest is one row.
 */
export interface Breakdown {
  mode: 'measured' | 'crew'
  rows: Row[]
  /** The request's characters; null when not split. */
  chars: number | null
  /** The window share the provider reported, 0..1, or null. */
  filled: number | null
  windowTokens: number
  /** Tokens in use: the window share times its size, or the harness's own count. */
  usedTokens: number
  /** Characters by which Kiro Crew's blocks exceed the engine's history; Messages
   *  is then shown as 0. Zero when they fit, which is the normal case. */
  overlap: number
}

const size = (r: Row) => r.chars ?? 0
const bySize = (a: Row, b: Row) => size(b) - size(a)
const kept = (rows: Row[]) => rows.filter(r => size(r) > 0).sort(bySize)

/**
 * One row where two would say the same thing: a parent whose only child has the
 * same number becomes that child (a split line just disappears).
 */
export function simplify(row: Row): Row {
  const kids = (row.children ?? []).map(simplify)
  if (kids.length === 1 && row.chars !== null && kids[0].chars === row.chars && !row.collapsible) {
    if (kids[0].plain) return { ...row, children: undefined }
    return { ...kids[0], share: row.share, count: row.count ?? kids[0].count }
  }
  return { ...row, children: kids.length > 0 ? kids : undefined }
}

function splitRows(id: string, split: Record<string, number> | undefined): Row[] | undefined {
  if (!PART_KEY[id] || !split) return undefined
  const rows = kept(
    PART_KEY[id]
      .filter(([part]) => typeof split[part] === 'number')
      .map(([part, key]) => ({ id: `${id}.${part}`, label: i18nT(key), chars: split[part], share: 0, color: '', plain: true })),
  )
  return rows.length > 0 ? rows : undefined
}

function crewRow(latest: LatestTurn, share: (n: number) => number): Row {
  const children = kept(
    CREW_PARTS.map(p => {
      const n = Math.max(0, latest.ours[p.cat] ?? 0)
      return { id: `crew.${p.cat}`, label: i18nT(p.labelKey), chars: n, share: share(n), color: `var(--ctx-cat-${p.cat})` }
    }),
  )
  const chars = children.reduce((s, r) => s + size(r), 0)
  return { id: 'crew', label: i18nT('pages.contextBreakdown.group_crew'), chars, share: share(chars), color: CREW_COLOR, children, collapsible: true }
}

/**
 * Why the rest of the context has no split, and the exact place to get one: the
 * Settings page, its Agent Harness tab, and the Kiro Agent choice, by the names the
 * UI shows. Plain text: the info tip renders text only, so the path is named.
 */
export function restTip(harnessName: string): string {
  return i18nT('pages.contextBreakdown.rest_tip', {
    harness: harnessName || i18nT('pages.contextBreakdown.rest_tip_this_agent'),
    settings: i18nT('nav.settings'),
    tab: i18nT('settings.tabs.agent.label'),
    agent: i18nT('pages.developer.agentBackendTab.kas_kiro_agent'),
  })
}

/**
 * The tokens left for "Not split": the total less this app's estimate, never below
 * 0. `over` is how far the estimate went past the total (0 when it fits).
 */
export function remainderTokens(total: number, estimate: number): { tokens: number; over: number } {
  const left = total - estimate
  return { tokens: Math.max(0, left), over: left < 0 ? -left : 0 }
}

/**
 * The newest turn as one flat list, zero rows hidden. Row 1 is always Kiro Crew
 * (rules, memory, skills, other). Then, largest first: Messages (the engine's
 * history less Kiro Crew's blocks: Your messages, Replies, Reasoning, Summary, Other
 * user text) and every other category the engine counted.
 */
export function breakdownOf(
  latest: LatestTurn,
  unknownNames: string[] = [],
  charsPerToken = 4,
  /** The session's agent, by the name the Agent Harness tab shows; empty when unknown. */
  harnessName = '',
  /** The session's harness id. On `kas` the engine measures every turn, so an unsplit
   *  turn is one whose measurement has not arrived yet, never one to switch away from.
   *  null when it could not be read: no advice is given about a harness not known. */
  harnessId: string | null = '',
  /** A past turn: on `kas` it was not measured, rather than not measured yet. */
  past = false,
): Breakdown {
  const message = Math.max(0, latest.ours.message ?? 0)
  const engine = latest.engine
  const yours = (share: number): Row => ({ id: 'crew.message', label: i18nT('pages.contextBreakdown.cat_message'), chars: message, share, color: MESSAGE_COLOR })
  if (!engine) {
    const windowTokens = latest.context_window > 0 ? latest.context_window : 0
    const filled = latest.context_used > 0 && windowTokens > 0 ? Math.min(1, latest.context_used / windowTokens) : null
    const crew = crewRow(latest, () => 0)
    const msg = yours(0)
    const headline = latest.context_used > 0 ? latest.context_used : 0
    if (headline <= 0) {
      // No token total: the rows stay in characters, and nothing else is claimed.
      return { mode: 'crew', rows: [crew, msg].filter(r => size(r) > 0), chars: null, filled, windowTokens, usedTokens: 0, overlap: 0 }
    }
    // Every row in tokens, so the rows add up to the total above: Kiro Crew's blocks
    // and the user's words at the composer's ratio, and the rest is what is left.
    const tag = (r: Row): Row => {
      const tokens = Math.round(size(r) / Math.max(0.1, charsPerToken))
      return { ...r, tokens, estimated: true, share: tokens / headline }
    }
    const kids = (crew.children ?? []).map(tag)
    const crewTokens = kids.reduce((s, r) => s + (r.tokens ?? 0), 0)
    const crewRowTok: Row = { ...crew, children: kids, tokens: crewTokens, estimated: true, share: crewTokens / headline }
    const msgRow = tag(msg)
    const left = remainderTokens(headline, crewTokens + (msgRow.tokens ?? 0))
    const rows = [crewRowTok, msgRow].filter(r => size(r) > 0)
    rows.push({
      id: 'rest',
      label: i18nT('pages.contextBreakdown.rest_label'),
      chars: null,
      tokens: left.tokens,
      estimated: true,
      share: left.tokens / headline,
      color: UNSPLIT_COLOR,
      // Why this part has no split: not measured yet on Kiro Agent, and elsewhere
      // where to get a split. With the harness unread, the error above says why.
      tip:
        harnessId === null
          ? undefined
          : harnessId === 'kas'
            ? past
              ? i18nT('pages.contextBreakdown.rest_tip_not_measured')
              : i18nT('pages.contextBreakdown.rest_tip_not_yet')
            : restTip(harnessName),
      // Named before any hover: a turn waiting on its measurement, or where to get one.
      tipTag:
        harnessId === null
          ? undefined
          : harnessId === 'kas'
            ? past
              ? i18nT('pages.contextBreakdown.rest_tag_past')
              : i18nT('pages.contextBreakdown.rest_tag_pending')
            : i18nT('pages.contextBreakdown.rest_tag_switch'),
    })
    return { mode: 'crew', rows, chars: null, filled, windowTokens, usedTokens: headline, overlap: left.over }
  }

  const sum = Object.values(engine.categories).reduce((s, v) => s + v, 0) + engine.unknown_chars
  const total = engine.total_chars ?? sum
  const pct = engine.percent ?? {}
  const share = (n: number) => n / Math.max(1, total)
  // The engine's own percent; one it rounded to 0 for a part that is there reads
  // as that part's real share, so the row says <0.1% rather than 0%.
  const wire = (id: string, n: number) => (typeof pct[id] === 'number' && (pct[id] > 0 || n === 0) ? pct[id] / 100 : share(n))

  // Kiro Crew's blocks are part of the engine's history; they get their own row, and
  // Messages is what the history holds besides them, so every character is counted once.
  const crew = crewRow(latest, share)
  const historyChars = engine.categories.history ?? 0
  // Your messages sits on its own row, right after Kiro Crew, on every turn.
  const rawMessages = historyChars - size(crew) - message
  const messagesChars = Math.max(0, rawMessages)
  const messageKids: Row[] = []
  for (const [part, key, color] of HISTORY_TAIL) {
    const n = engine.history[part]
    if (typeof n === 'number') messageKids.push({ id: `history.${part}`, label: i18nT(key), chars: n, share: share(n), color })
  }
  messageKids.push({
    id: 'history.user_other',
    label: i18nT('pages.contextBreakdown.engine_hist_user_other'),
    chars: engine.user_other_chars ?? 0,
    share: share(engine.user_other_chars ?? 0),
    color: 'var(--ctx-eng-history-user)',
  })
  const others: Row[] = [
    {
      id: 'history',
      label: i18nT('pages.contextBreakdown.engine_cat_history'),
      chars: messagesChars,
      share: share(messagesChars),
      color: 'var(--ctx-eng-history-user)',
      children: kept(messageKids),
    },
  ]
  for (const [id, chars] of Object.entries(engine.categories)) {
    if (id === 'history') continue
    others.push({
      id,
      label: CATEGORY_KEY[id] ? i18nT(CATEGORY_KEY[id]) : id,
      chars,
      share: wire(id, chars),
      color: CATEGORY_KEY[id] ? `var(--ctx-eng-${id})` : 'var(--ctx-eng-other)',
      count: engine.counts?.[id],
      children: splitRows(id, engine.parts?.[id]),
    })
  }
  if (engine.unknown_chars > 0) {
    others.push({
      id: 'unknown',
      label: unknownNames.length > 0 ? i18nT('pages.contextBreakdown.engine_unknown_named', { names: fmtList(unknownNames) }) : i18nT('pages.contextBreakdown.engine_unknown'),
      chars: engine.unknown_chars,
      share: share(engine.unknown_chars),
      color: 'var(--ctx-eng-unknown)',
    })
  }
  const win = engine.window
  const windowTokens = win?.size_tokens ?? 0
  const filled = typeof win?.usage_percent === 'number' && windowTokens > 0 ? Math.min(1, Math.max(0, win.usage_percent / 100)) : null
  return {
    mode: 'measured',
    // Kiro Crew is always row 1; everything else follows by size.
    rows: [...[crew, yours(share(message))].filter(r => size(r) > 0), ...kept(others).map(simplify)],
    chars: total,
    filled,
    windowTokens,
    usedTokens: filled !== null ? Math.round(filled * windowTokens) : 0,
    overlap: rawMessages < 0 ? -rawMessages : 0,
  }
}

/** The bar's parts: Kiro Crew's blocks first (the last one marks the end of the
 *  group), then every other top-level row as one part, in list order. */
export function barSegments(rows: Row[]): Row[] {
  const out: Row[] = []
  for (const row of rows) {
    if (row.id === 'crew') {
      const kids = (row.children ?? []).filter(c => (c.tokens ?? size(c)) > 0)
      const parts = kids.length > 0 ? kids : [row]
      parts.forEach((p, i) => out.push(i === parts.length - 1 ? { ...p, groupEnd: true } : p))
    } else if ((row.tokens ?? size(row)) > 0) {
      out.push(row)
    }
  }
  return out
}

/** "4% of the 1M window", or null without a window reading. */
export function windowLine(b: Breakdown): string | null {
  if (b.filled === null || b.windowTokens <= 0) return null
  return i18nT('pages.contextBreakdown.window_usage', {
    percent: fmtPercent(b.filled, { maximumFractionDigits: b.filled < 0.01 ? 1 : 0 }),
    size: fmtTokensShort(b.windowTokens),
  })
}

// --------------------------------------------------------------------------- //
// Rendering
// --------------------------------------------------------------------------- //

function Swatch({ color }: { color: string }) {
  return <i className="w-2.5 h-2.5 rounded-[2px] shrink-0" style={{ background: color }} aria-hidden="true" />
}

/** The request, full width: each part at its share of the characters. */
function Bar({
  segments,
  hover,
  setHover,
  window: filled,
}: {
  segments: Row[]
  hover: string | null
  setHover: (id: string | null) => void
  /** Draw against the whole window, filled to this share; absent: the request at full width. */
  window?: number
}) {
  return (
    <div
      className={`flex h-4 w-full overflow-hidden rounded-[4px] ${filled !== undefined ? 'bg-[var(--border)]' : 'bg-[var(--card)]'}`}
      role="img"
      aria-label={fmtList(segments.map(s => `${s.label} ${fmtShare(s.share)}`))}
      data-testid="engine-bar"
      // A pointer-only readout: every number is also in the list below, which is
      // what the keyboard and assistive technology read.
      onPointerLeave={() => setHover(null)}
    >
      {segments.map(s => (
        <span
          key={s.id}
          data-engine-seg={s.id}
          className={`h-full min-w-[2px] transition-opacity ${hover && hover !== s.id ? 'opacity-60' : ''}`}
          style={{
            ...(filled !== undefined
              ? { width: `${filled * s.share * 100}%`, minWidth: 2, flexShrink: 0 }
              : { flex: `${Math.max(s.share, 0.0005)} 1 0px` }),
            background: s.color,
            boxShadow: 'inset -1px 0 0 var(--card)',
            marginRight: s.groupEnd ? 3 : 0,
          }}
          onPointerEnter={() => setHover(s.id)}
        />
      ))}
    </div>
  )
}

/** The window: a thin bar filled to the provider's percent, and its line. */
function WindowBar({ breakdown, lineOnly }: { breakdown: Breakdown; lineOnly?: boolean }) {
  const line = windowLine(breakdown)
  if (!line || breakdown.filled === null) return null
  return (
    <div className="mt-2" data-testid="window-bar">
      {lineOnly ? null : (
        <div className="h-1.5 w-full rounded-full bg-[var(--border)] overflow-hidden">
          <div className="h-full rounded-full bg-[var(--muted)]" style={{ width: `${breakdown.filled * 100}%`, minWidth: 2 }} />
        </div>
      )}
      <div className="mt-1 text-[12px] text-muted tabular-nums" data-testid="window-usage">
        {line}
      </div>
    </div>
  )
}

function RowView({
  row,
  top,
  hover,
  measured,
  open,
  toggle,
}: {
  row: Row
  top: boolean
  hover: string | null
  measured: boolean
  open: Set<string>
  toggle: (id: string) => void
}) {
  const kids = row.children ?? []
  const shut = row.collapsible && !open.has(row.id)
  const Chevron = shut ? ChevronRight : ChevronDown
  const swatch = row.plain ? <i className="w-2.5 shrink-0" aria-hidden="true" /> : <Swatch color={row.color} />
  const words = (
    <>
      {/* Narrow panel: the label wraps rather than being cut; wide: one line. */}
      <span className="min-w-0 break-words @[26rem]/ctxrows:truncate" title={row.label} data-engine-label>
        {row.label}
      </span>
      {typeof row.count === 'number' && row.count > 0 ? (
        <span className="text-muted tabular-nums shrink-0" data-engine-count>
          {i18nT('pages.contextBreakdown.list_count', { count: row.count, n: fmtN(row.count) })}
        </span>
      ) : null}
      {row.collapsible && kids.length > 0 ? <Chevron size={14} className="lucide-inline shrink-0 text-muted" aria-hidden="true" /> : null}
    </>
  )
  const label = (
    <>
      {swatch}
      {words}
    </>
  )
  const line = (
    <div
      data-engine-row={row.id}
      className={`flex items-center justify-between gap-3 rounded-sm ${top ? 'py-2 text-[13px]' : 'py-1 text-[12px]'} text-text ${hover === row.id ? 'bg-[var(--card-hl)]' : ''}`}
    >
      {row.collapsible && kids.length > 0 ? (
        <button
          type="button"
          className="flex items-center gap-2 min-w-[5rem] flex-1 text-left bg-transparent border-0 p-0 appearance-none cursor-pointer text-inherit hover:text-text-strong rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
          aria-expanded={!shut}
          onClick={() => toggle(row.id)}
        >
          {label}
        </button>
      ) : (
        <span className="flex items-center gap-2 min-w-[5rem] flex-1">
          {swatch}
          {/* Narrow panel: the tip's tag and glyph go under the label rather than
              squeezing it; the swatch stays beside the first line. */}
          <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 min-w-0">
            {words}
            {row.tip ? (
              <span className="shrink-0 inline-flex items-center gap-1" data-engine-tip>
                {row.tipTag ? (
                  <span className="text-[11px] text-muted whitespace-nowrap" data-engine-tip-tag>
                    {row.tipTag}
                  </span>
                ) : null}
                {/* Above the glyph, starting at it: never over the row's own label. */}
                <InfoTip text={row.tip} placement="top" />
              </span>
            ) : null}
          </span>
        </span>
      )}
      <span className="flex items-center gap-2 shrink-0">
        <span className="font-mono text-[12px] text-muted tabular-nums">
          {row.tokens !== undefined ? (
            // Narrow panel: the grey characters go under the tokens, so the label
            // keeps its room; wide: they sit beside them.
            <span className="flex flex-col items-end @[26rem]/ctxrows:flex-row @[26rem]/ctxrows:items-baseline" data-engine-value>
              <span className="whitespace-nowrap">
                {row.estimated ? '~' : ''}
                {fmtN(row.tokens)} {i18nT('pages.contextBreakdown.unit_tokens')}
              </span>
              {row.chars !== null ? (
                <span className="font-sans text-[11px] opacity-70 whitespace-nowrap @[26rem]/ctxrows:ml-1.5" data-engine-chars>
                  {fmtN(row.chars)} {i18nT('pages.contextBreakdown.unit_chars')}
                </span>
              ) : null}
            </span>
          ) : row.chars === null ? (
            ''
          ) : measured || row.plain ? (
            fmtN(row.chars)
          ) : (
            `${fmtN(row.chars)} ${i18nT('pages.contextBreakdown.unit_chars')}`
          )}
        </span>
        {measured || row.tokens !== undefined ? (
          <span className="font-mono text-[12px] text-text tabular-nums w-12 text-right" data-engine-pct>
            {row.plain ? '' : fmtShare(row.share)}
          </span>
        ) : null}
      </span>
    </div>
  )
  const nested =
    kids.length > 0 && !shut ? (
      <div className="mb-1 ml-1 pl-4 border-l-2 border-border">
        {kids.map(child => (
          <RowView key={child.id} row={child} top={false} hover={hover} measured={measured} open={open} toggle={toggle} />
        ))}
      </div>
    ) : null
  return top ? (
    <div className="border-b border-border">
      {line}
      {nested}
    </div>
  ) : (
    <>
      {line}
      {nested}
    </>
  )
}

/** The media line: images and documents the request carried, outside the total. */
function mediaText(media: EngineMeasurement['media']): string | null {
  if (!media) return null
  const parts: string[] = []
  if ((media.images ?? 0) > 0) {
    parts.push(i18nT('pages.contextBreakdown.media_images', { count: media.images ?? 0, n: fmtN(media.images ?? 0), size: fmtBytes(media.image_bytes ?? 0) }))
  }
  if ((media.documents ?? 0) > 0) {
    parts.push(i18nT('pages.contextBreakdown.media_documents', { count: media.documents ?? 0, n: fmtN(media.documents ?? 0), size: fmtBytes(media.document_bytes ?? 0) }))
  }
  return parts.length > 0 ? fmtList(parts) : null
}

/** The turn's one size, as the headline states it: the request's characters when the
 *  engine measured it, else the harness's tokens; null when neither was reported. */
export function headlineFigure(breakdown: Breakdown): { n: number; unit: 'chars' | 'tokens' } | null {
  if (breakdown.mode === 'measured') return breakdown.chars === null ? null : { n: breakdown.chars, unit: 'chars' }
  return breakdown.usedTokens > 0 ? { n: breakdown.usedTokens, unit: 'tokens' } : null
}

/** The headline: characters and about how many tokens, or the harness's tokens. */
export function Headline({ breakdown }: { breakdown: Breakdown }) {
  const measured = breakdown.mode === 'measured'
  const big = headlineFigure(breakdown)?.n ?? null
  if (big === null) {
    return (
      <div className="mt-1 text-[15px] text-muted" data-testid="whole-total">
        {i18nT('pages.contextBreakdown.total_unknown')}
      </div>
    )
  }
  return (
    <div className="mt-1 flex items-baseline gap-1.5 flex-wrap tabular-nums">
      <span className="text-[28px] font-semibold tracking-tight text-text-strong" data-testid="whole-total">
        {fmtN(big)}
      </span>
      <span className="text-[13px] text-muted" data-testid="whole-unit">
        {measured ? i18nT('pages.contextBreakdown.unit_chars') : i18nT('pages.contextBreakdown.unit_tokens')}
        {measured && breakdown.usedTokens > 0 ? ` · ${i18nT('pages.contextBreakdown.tokens_about', { n: fmtN(breakdown.usedTokens) })}` : ''}
      </span>
    </div>
  )
}

/** The newest turn: the request bar, the window bar, the tree, one short line. */
export function TurnBreakdown({
  breakdown,
  engine,
  ttft,
  charsPerToken,
}: {
  breakdown: Breakdown
  engine: EngineMeasurement | null | undefined
  ttft: EngineTtft | null | undefined
  charsPerToken?: number | null
}) {
  // A turn told in tokens draws its bar against the window; the request bar is for
  // a measured turn only.
  const inTokens = breakdown.mode === 'crew' && breakdown.usedTokens > 0 && breakdown.filled !== null
  const [hover, setHover] = useState<string | null>(null)
  const [open, setOpen] = useState<Set<string>>(() => new Set(['crew']))
  const toggle = (id: string) =>
    setOpen(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const measured = breakdown.mode === 'measured'
  const segments = measured || inTokens ? barSegments(breakdown.rows) : []
  const hovered = segments.find(s => s.id === hover)
  const media = measured ? mediaText(engine?.media) : null
  const caption = measured
    ? [i18nT('pages.contextBreakdown.caption_measured'), ttft ? i18nT('pages.contextBreakdown.engine_ttft', { total: fmtSeconds(ttft.total_ms) }) : ''].filter(Boolean).join(' ')
    : inTokens
      ? i18nT('pages.contextBreakdown.caption_estimate', { ratio: fmtNumber(charsPerToken ?? 4) })
      : i18nT('pages.contextBreakdown.caption_chars')
  let notes: ReactNode = null
  if (engine) {
    notes = (
      <>
        {engine.mismatch_chars > 0 && engine.mismatch_chars * 100 >= (engine.history.user ?? 0) ? (
          <p className="m-0 mt-2 text-[12px] text-[var(--warn)]" data-testid="engine-mismatch">
            {engine.compacted || (engine.history.compaction_summary ?? 0) > 0
              ? i18nT('pages.contextBreakdown.engine_mismatch_compacted', { chars: fmtN(engine.mismatch_chars) })
              : i18nT('pages.contextBreakdown.engine_mismatch', { chars: fmtN(engine.mismatch_chars) })}
          </p>
        ) : null}
        {engine.approximate ? (
          <p className="m-0 mt-2 text-[12px] text-muted" data-testid="engine-approximate">
            {i18nT('pages.contextBreakdown.engine_approximate')}
          </p>
        ) : null}
        {!engine.schema_known ? (
          <p className="m-0 mt-2 text-[12px] text-muted" data-testid="engine-schema-unknown">
            {i18nT('pages.contextBreakdown.engine_schema_unknown')}
          </p>
        ) : null}
      </>
    )
  }
  return (
    <div className="mt-3" data-testid="engine-breakdown" data-breakdown-mode={breakdown.mode}>
      {segments.length > 0 ? (
        <>
          {/* The hover readout has its own line above the bar, so it never covers text. */}
          <div className="h-5 flex items-center gap-1.5 text-[12px] tabular-nums" data-testid="engine-bar-tip" aria-live="polite">
            {hovered ? (
              <>
                <Swatch color={hovered.color} />
                <span className="text-text-strong truncate">{hovered.label}</span>
                <span className="font-mono text-muted shrink-0">
                  {hovered.tokens !== undefined
                    ? `${hovered.estimated ? '~' : ''}${fmtN(hovered.tokens)} ${i18nT('pages.contextBreakdown.unit_tokens')}`
                    : fmtN(size(hovered))}{' '}
                  · {fmtShare(hovered.share)}
                </span>
              </>
            ) : null}
          </div>
          <Bar segments={segments} hover={hover} setHover={setHover} window={inTokens ? (breakdown.filled ?? 0) : undefined} />
        </>
      ) : null}
      <WindowBar breakdown={breakdown} lineOnly={inTokens} />

      <div className="mt-2 @container/ctxrows" data-testid="engine-rows">
        {breakdown.rows.map(row => (
          <RowView key={row.id} row={row} top hover={hover} measured={measured} open={open} toggle={toggle} />
        ))}
        {media ? (
          <div className="flex items-center justify-between gap-3 py-2 border-b border-border text-[13px] text-text" data-testid="engine-media">
            <span>{i18nT('pages.contextBreakdown.media_label')}</span>
            <span className="font-mono text-[12px] text-muted tabular-nums">{media}</span>
          </div>
        ) : null}
      </div>
      {notes}
      {breakdown.overlap > 0 ? (
          <p className="m-0 mt-2 text-[12px] text-[var(--warn)]" data-testid="engine-overlap">
            {measured
              ? i18nT('pages.contextBreakdown.messages_overlap', { chars: fmtN(breakdown.overlap) })
              : i18nT('pages.contextBreakdown.estimate_over', { n: fmtN(breakdown.overlap) })}
          </p>
        ) : null}
      <p className="m-0 mt-2 text-[12px] text-muted" data-testid="engine-caption">
        {caption}
      </p>
    </div>
  )
}
