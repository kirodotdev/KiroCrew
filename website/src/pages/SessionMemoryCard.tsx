/**
 * Session & Task Memory — a task-manager view of "which session is using my RAM".
 *
 * The System page already answers the aggregate question (host total, gateway
 * pool, RAM saved). What it could not answer is the per-session breakdown, which
 * is the one a user actually acts on: they close a session, not a byte total.
 *
 * Shaped after Activity Monitor rather than the dashboard's usual stat cards: a
 * dense table beats N cards when the task is "find the biggest one", and per-row
 * bars turn a column of numbers into noise. Rows disclose their running tasks,
 * and clicking a session opens its chat window — the row is only useful if it
 * leads somewhere.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, ChevronUp, MemoryStick } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Card, CardTitle, EmptyState, IconButton, SearchInput } from '../components/ui'
import InfoTip from '../components/InfoTip'
import { compareText, fmtDuration, fmtNumber, fmtPercent, fmtUnit, type FormatUnit } from '../i18n/format'

import { i18nT } from '../i18n/t'
type Payload = Awaited<ReturnType<typeof api.sessionsMemory>>
type SessionRow = Payload['sessions'][number]
type TaskRow = Payload['tasks'][number]

/** A display row: a session, or one of its tasks indented beneath it. */
export interface DisplayRow {
  kind: 'session' | 'task'
  id: string
  name: string
  agent: string
  rssMb: number | null
  peakMb: number | null
  cpuCores: number | null
  procs: number | null
  mcp: number | null
  uptimeS: number | null
  pid: number | null
  shared: boolean
  /** True when this session owns tasks, i.e. the row has a disclosure triangle. */
  hasTasks: boolean
  /** Disclosure state; meaningless on task rows. */
  expanded: boolean
  /** Route to open on click, or null when this row has no chat window. */
  href: string | null
}

/**
 * Route that opens a session's chat window, or null when there is none to open.
 *
 * Only dashboard sessions have a chat window. `_bg`, cron, and Slack sessions are
 * real sessions with real memory, but nothing to navigate to — returning null
 * keeps them in the table as non-interactive rows instead of shipping a click
 * that silently does nothing.
 *
 * ChatPage resolves the session from the `?sid=` query param and dispatches
 * `switchSlot` itself, so navigation alone is sufficient. The param takes the
 * BARE slot key: the `dashboard:` prefix belongs to the backend session key.
 */
export function sessionChatPath(sessionKey: string): string | null {
  if (!sessionKey.startsWith('dashboard:')) return null
  const slotKey = sessionKey.slice('dashboard:'.length)
  if (!slotKey) return null
  return `/chat?sid=${encodeURIComponent(slotKey)}`
}

/** `3238` -> `"3,238.0MB"` in the active locale; null/unsampled -> em dash. */
export function fmtMb(mb: number | null | undefined): string {
  if (mb == null || !Number.isFinite(mb)) return '—'
  return fmtUnit(mb, 'megabyte', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
}

/**
 * Host-scale totals as a bare number of GB. The per-row table stays in MB so its
 * column is directly comparable, but host totals in MB read as six digits.
 *
 * Deliberately unit-LESS: the unit belongs in the label's catalog string, not
 * here. `fmtUnit(x, 'gigabyte')` emits a literal "GB" that no catalog owns, so
 * under the pseudolocale it renders as untranslated Latin glued to its label —
 * which is exactly what the i18n render gate flags (latin-leak/untranslated-text).
 */
export function fmtGb(mb: number | null | undefined): string {
  if (mb == null || !Number.isFinite(mb)) return '—'
  return fmtNumber(mb / 1024, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/**
 * History samples -> one bar height per sample, as a percentage of the host total.
 *
 * The y-axis is scaled to the host total, not to the series max, so a bar's height
 * means "share of this machine" rather than "share of its own peak" — a
 * self-scaled trace makes 200 MB of churn look like a crisis. A flat row of short
 * bars is the correct picture of an idle machine.
 *
 * Bars rather than a vector path because code-review.yml blocks inline vector
 * markup outside the brand marks, and this needs no path math to read.
 */
export function sparklineBars(
  history: Array<{ t: number; mb: number }>,
  hostMb: number | null,
): number[] {
  if (history.length < 2) return []
  const ceiling = hostMb && hostMb > 0 ? hostMb : Math.max(...history.map(h => h.mb)) || 1
  return history.map(h => Math.min(100, Math.max(0, (h.mb / ceiling) * 100)))
}

/** Share of host RAM. Takes MB on both sides and hands the RATIO to Intl. */
export function fmtHostPct(mb: number | null | undefined, hostMb: number | null): string {
  if (mb == null || !hostMb) return '—'
  return fmtPercent(mb / hostMb, { maximumFractionDigits: 2 })
}

/**
 * Uptime as a compound duration. Coarse on purpose: once a session is days old,
 * its seconds are noise, and a fixed `HH:MM:SS` clock is not a duration format
 * any locale agrees on.
 */
export function fmtUptime(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  const s = Math.floor(seconds)
  const parts: Array<[number, FormatUnit]> = [
    [Math.floor(s / 86400), 'day'],
    [Math.floor((s % 86400) / 3600), 'hour'],
    [Math.floor((s % 3600) / 60), 'minute'],
  ]
  return fmtDuration(parts, { dropZero: true, maximumFractionDigits: 0 })
}

/**
 * A session with no generated title yet must still be distinguishable — every
 * such row would otherwise read identically — so the slot key disambiguates it.
 */
export function rowName(row: SessionRow): string {
  if (row.untitled && row.slot_key) return `${row.title} ${row.slot_key}`
  return row.title || row.key
}

/**
 * Flatten sessions + tasks into display order: sessions by the chosen column,
 * each immediately followed by its own tasks, always by memory descending.
 *
 * Sorts unsampled rows (null memory) last rather than treating them as 0, so a
 * session still being measured does not masquerade as the smallest one. Tasks
 * stay welded under their parent instead of joining the global sort — a task's
 * number is only meaningful next to the session that owns it.
 */
export type SortKey = 'name' | 'rssMb' | 'peakMb' | 'procs' | 'mcp' | 'cpuCores' | 'uptimeS' | 'agent' | 'pid'

/**
 * Which slice of the same table is on screen.
 *
 * `Network` is deliberately absent even though Activity Monitor has one: nothing
 * in the payload attributes bytes to a session, and an always-empty tab is worse
 * than no tab.
 */
export type View = 'memory' | 'cpu' | 'tasks'

/** Columns each view shows. Sorting stays available on all of them. */
export const VIEW_COLUMNS: Record<View, SortKey[]> = {
  memory: ['name', 'rssMb', 'peakMb', 'procs', 'mcp', 'cpuCores', 'uptimeS', 'agent', 'pid'],
  cpu: ['name', 'cpuCores', 'rssMb', 'procs', 'uptimeS', 'agent', 'pid'],
  tasks: ['name', 'rssMb', 'peakMb', 'cpuCores', 'uptimeS', 'agent', 'pid'],
}

/** The column a view opens on, so switching tab immediately ranks by its subject. */
export const VIEW_SORT: Record<View, SortKey> = { memory: 'rssMb', cpu: 'cpuCores', tasks: 'rssMb' }

export function buildRows(
  sessions: SessionRow[],
  tasks: TaskRow[],
  sort: { key: SortKey; desc: boolean } = { key: 'rssMb', desc: true },
  filter = '',
  view: View = 'memory',
  collapsed: ReadonlySet<string> = new Set(),
): DisplayRow[] {
  const byMemDesc = (a: number | null, b: number | null): number => {
    if (a == null && b == null) return 0
    if (a == null) return 1
    if (b == null) return -1
    return b - a
  }
  // Nulls sort last in BOTH directions: "unknown" is not a small value, and
  // flipping the column must not promote unmeasured rows to the top.
  const cmp = (a: DisplayRow, b: DisplayRow): number => {
    const av = a[sort.key] as number | string | null
    const bv = b[sort.key] as number | string | null
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    // compareText, not localeCompare: a bare localeCompare collates by the
    // BROWSER's locale, so a translated UI would sort its text columns in the
    // wrong language (src/i18n/localeFormatting.test.ts polices this).
    const d = typeof av === 'string' && typeof bv === 'string' ? compareText(av, bv) : Number(av) - Number(bv)
    return sort.desc ? -d : d
  }
  const needle = filter.trim().toLowerCase()
  const matches = (name: string, agent: string): boolean =>
    !needle || name.toLowerCase().includes(needle) || agent.toLowerCase().includes(needle)
  const taskRow = (t: TaskRow, kind: 'task' | 'session'): DisplayRow => ({
    kind,
    id: t.id,
    name: t.task,
    agent: t.agent,
    rssMb: t.sampled ? t.rss_mb : null,
    peakMb: t.sampled ? t.peak_rss_mb : null,
    cpuCores: t.sampled ? t.cpu_cores : null,
    procs: null,
    mcp: null,
    uptimeS: t.started_at ? Date.now() / 1000 - t.started_at : null,
    pid: t.pid,
    shared: t.shared,
    hasTasks: false,
    expanded: false,
    href: null,
  })

  // The Tasks view is a flat ranking, not an outline: the question there is
  // "which task is expensive", which a parent grouping actively obscures.
  if (view === 'tasks') {
    return tasks
      .filter(t => matches(t.task, t.agent))
      .map(t => taskRow(t, 'task'))
      .sort(cmp)
  }

  const asRow = (s: SessionRow): DisplayRow => ({
    kind: 'session',
    id: s.key,
    name: rowName(s),
    agent: s.agent,
    rssMb: s.rss_mb,
    peakMb: null,
    cpuCores: s.cpu_cores,
    procs: s.procs,
    mcp: s.mcp,
    uptimeS: s.uptime_s,
    pid: s.pid,
    shared: !s.owns_runtime,
    hasTasks: tasks.some(t => t.parent === s.key),
    expanded: !collapsed.has(s.key),
    href: sessionChatPath(s.key),
  })
  const out: DisplayRow[] = []
  const visible = sessions
    .map(asRow)
    .filter(r => matches(r.name, r.agent))
    .sort(cmp)
  for (const row of visible) {
    out.push(row)
    if (!row.expanded) continue
    const mine = tasks
      .filter(t => t.parent === row.id)
      .sort((a, b) => byMemDesc(a.sampled ? a.rss_mb : null, b.sampled ? b.rss_mb : null))
    for (const t of mine) out.push(taskRow(t, 'task'))
  }
  return out
}

const NUM_CELL = 'px-3 py-1.5 text-right font-mono text-[12.5px] tabular-nums whitespace-nowrap'
const HEAD_BASE = 'px-3 py-1.5 text-[11px] font-medium text-muted whitespace-nowrap'
// Two separate constants rather than appending `text-left` to a right-aligned
// base: Tailwind resolves conflicting utilities by CSS source order, not by the
// order they appear in the class string, so `text-right text-left` silently kept
// the RIGHT alignment and the two text headers sat over left-aligned data.
const HEAD_CELL = `${HEAD_BASE} text-right`
const HEAD_CELL_L = `${HEAD_BASE} text-left`

export default function SessionMemoryCard() {
  const navigate = useNavigate()
  const [view, setView] = useState<View>('memory')
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: 'rssMb', desc: true })
  const [filter, setFilter] = useState('')
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const { data } = useQuery<Payload>({
    queryKey: ['sessionsMemory'],
    queryFn: () => api.sessionsMemory(),
    refetchInterval: 5000,
  })

  const sessions = data?.sessions ?? []
  const tasks = data?.tasks ?? []
  const totals = data?.totals
  const history = data?.history ?? []
  const hostMb = totals?.host_mb ?? null
  const rows = buildRows(sessions, tasks, sort, filter, view, collapsed)
  const cols = VIEW_COLUMNS[view]
  const has = (c: SortKey) => cols.includes(c)

  const usedMb = totals?.rss_mb ?? 0
  const largestMb = sessions.reduce<number | null>(
    (m, s) => (s.rss_mb != null && (m == null || s.rss_mb > m) ? s.rss_mb : m),
    null,
  )
  const procTotal = sessions.reduce((n, s) => n + (s.procs ?? 0), 0)
  const mcpTotal = sessions.reduce((n, s) => n + (s.mcp ?? 0), 0)
  const bars = sparklineBars(history, hostMb)

  const open = (href: string | null) => {
    if (href) navigate(href)
  }

  const switchView = (v: View) => {
    setView(v)
    setSort({ key: VIEW_SORT[v], desc: true })
  }

  const toggle = (key: string) =>
    setCollapsed(prev => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })

  // Re-sorting is THE interaction on a task-manager table ("which one is
  // biggest?"), so every column is a button. Clicking the active column flips
  // direction; a new column starts descending, which is what you want for every
  // numeric column and is harmless for the two text ones.
  const Head = ({ col, label, left }: { col: SortKey; label: string; left?: boolean }) => (
    <th
      className={left ? HEAD_CELL_L : HEAD_CELL}
      aria-sort={sort.key === col ? (sort.desc ? 'descending' : 'ascending') : 'none'}
    >
      <Btn
        type="button"
        onClick={() => setSort(s => (s.key === col ? { key: col, desc: !s.desc } : { key: col, desc: true }))}
        // `Btn` rather than a raw <button>, and Lucide chevrons rather than
        // ▾/▴ glyphs: a text symbol ignores `currentColor` and the theme
        // tokens, so it would not follow the accent the active column is
        // marked with, and it renders differently on each platform.
        //
        // The overrides strip Btn's border, fill and 13px body type — a boxed
        // control in every column head would read as an action, not a label.
        className={`border-transparent bg-transparent px-0 py-0 gap-1 text-[11px] font-medium ${
          sort.key === col ? 'text-accent' : 'text-muted'
        }`}
      >
        {label}
        {sort.key === col &&
          (sort.desc ? (
            <ChevronDown size={12} aria-hidden="true" className="lucide-inline" />
          ) : (
            <ChevronUp size={12} aria-hidden="true" className="lucide-inline" />
          ))}
      </Btn>
    </th>
  )

  const Stat = ({ label, value }: { label: string; value: string }) => (
    <div className="flex items-baseline gap-2">
      <span className="text-[11.5px] text-muted whitespace-nowrap">{label}</span>
      <span className="text-[12.5px] font-mono tabular-nums text-text-strong">{value}</span>
    </div>
  )

  return (
    <Card className="mb-6">
      {/* docs/page-layout.md: data sections are Card + CardTitle + InfoTip and a
          `table-striped` table — never a hand-rolled wrapper. The measurement
          caveat lives in the InfoTip rather than as body copy so it does not
          cost two lines of vertical space above the data. */}
      <CardTitle>
        {i18nT('pages.sessionMemoryCard.session_task_memory')}
        <InfoTip text={i18nT('pages.sessionMemoryCard.resident_memory_of_each_session_s_whole_process')} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">
          {fmtMb(totals?.rss_mb ?? null)}
          {hostMb ? ` / ${fmtMb(hostMb)}` : ''}
          {totals?.host_pct != null ? ` · ${fmtPercent(totals.host_pct / 100, { maximumFractionDigits: 2 })}` : ''}
        </span>
      </CardTitle>

      {/* Activity Monitor's toolbar: the view switch and the filter share one
          row so the table starts as high as possible. */}
      <div className="mb-3 flex items-center gap-3 flex-wrap">
        <div role="tablist" className="inline-flex rounded border border-border overflow-hidden">
          {(
            [
              ['memory', i18nT('pages.sessionMemoryCard.memory')],
              ['cpu', i18nT('pages.sessionMemoryCard.cpu')],
              ['tasks', i18nT('pages.sessionMemoryCard.tasks')],
            ] as Array<[View, string]>
          ).map(([v, label]) => (
            <Btn
              key={v}
              type="button"
              role="tab"
              aria-selected={view === v}
              onClick={() => switchView(v)}
              // Overrides drop Btn's own rounded border so the three read as
              // one segmented strip, which the container already draws.
              className={`rounded-none border-transparent border-r border-r-border last:border-r-0 px-3 py-1 text-[12px] ${
                view === v
                  ? 'bg-bg-elevated text-text-strong font-medium'
                  : 'bg-transparent text-muted hover:text-text'
              }`}
            >
              {label}
            </Btn>
          ))}
        </div>
        <SearchInput
          placeholder={i18nT('pages.sessionMemoryCard.filter_sessions')}
          value={filter}
          onChange={e => setFilter(e.currentTarget.value)}
          className="ml-auto w-full sm:w-56"
        />
      </div>

      {rows.length === 0 ? (
        <EmptyState
          icon={<MemoryStick className="lucide-inline" />}
          title={i18nT('pages.sessionMemoryCard.no_active_sessions')}
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr className="bg-bg-elevated border-b border-border">
                <Head col="name" label={i18nT('pages.sessionMemoryCard.session_task')} left />
                {has('rssMb') && <Head col="rssMb" label={i18nT('pages.sessionMemoryCard.memory')} />}
                {has('rssMb') && <th className={HEAD_CELL}>{i18nT('pages.sessionMemoryCard.host_share')}</th>}
                {has('peakMb') && <Head col="peakMb" label={i18nT('pages.sessionMemoryCard.peak')} />}
                {has('procs') && <Head col="procs" label={i18nT('pages.sessionMemoryCard.proc')} />}
                {has('mcp') && <Head col="mcp" label={i18nT('pages.sessionMemoryCard.mcp')} />}
                {has('cpuCores') && <Head col="cpuCores" label={i18nT('pages.sessionMemoryCard.cpu')} />}
                {has('uptimeS') && <Head col="uptimeS" label={i18nT('pages.sessionMemoryCard.uptime')} />}
                <Head col="agent" label={i18nT('pages.sessionMemoryCard.agent')} left />
                <Head col="pid" label={i18nT('pages.sessionMemoryCard.pid')} />
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr
                  key={`${r.kind}:${r.id}`}
                  className={`border-b border-border/60 last:border-b-0 ${
                    r.href ? 'cursor-pointer hover:bg-bg-hover' : ''
                  }`}
                  {...(r.href ? { onClick: () => open(r.href) } : {})}
                >
                  <td
                    className={`px-3 py-1.5 text-left text-[12.5px] max-w-[330px] truncate ${
                      r.kind === 'task' ? 'pl-9 text-text' : 'text-text-strong font-medium'
                    }`}
                    title={r.name}
                  >
                    {/* Two SIBLING controls, never nested. Row-level click stays
                        as a mouse convenience, but the keyboard and
                        screen-reader path lives on these: a <tr role="button">
                        wrapping a second control is invalid interactive nesting,
                        and the earlier `aria-hidden` caret was unreachable
                        without a mouse — the disclosure simply did not exist for
                        assistive tech. */}
                    {r.hasTasks && (
                      <IconButton
                        aria-expanded={r.expanded}
                        aria-label={i18nT(
                          r.expanded
                            ? 'pages.sessionMemoryCard.collapse_tasks'
                            : 'pages.sessionMemoryCard.expand_tasks',
                          { name: r.name },
                        )}
                        onClick={e => {
                          e.stopPropagation()
                          toggle(r.id)
                        }}
                        className="inline-block w-3 -ml-3 mr-0.5 p-0 align-middle text-muted hover:text-text"
                      >
                        {r.expanded ? (
                          <ChevronDown size={12} aria-hidden="true" className="lucide-inline" />
                        ) : (
                          <ChevronRight size={12} aria-hidden="true" className="lucide-inline" />
                        )}
                      </IconButton>
                    )}
                    {r.href ? (
                      <Btn
                        type="button"
                        onClick={e => {
                          e.stopPropagation()
                          open(r.href)
                        }}
                        // Overrides strip Btn's box so the session name still
                        // reads as a name in a table cell rather than a button.
                        className="border-transparent bg-transparent px-0 py-0 text-left text-inherit font-inherit hover:underline"
                      >
                        {r.name}
                      </Btn>
                    ) : (
                      r.name
                    )}
                    {r.shared && (
                      <span className="ml-1.5 text-[10px] px-1.5 rounded border border-warn/40 text-warn align-[1px]">
                        {i18nT('pages.sessionMemoryCard.shared')}
                      </span>
                    )}
                  </td>
                  {has('rssMb') && <td className={NUM_CELL}>{fmtMb(r.rssMb)}</td>}
                  {has('rssMb') && <td className={NUM_CELL}>{fmtHostPct(r.rssMb, hostMb)}</td>}
                  {has('peakMb') && <td className={NUM_CELL}>{fmtMb(r.peakMb)}</td>}
                  {has('procs') && <td className={NUM_CELL}>{r.procs != null ? fmtNumber(r.procs) : '—'}</td>}
                  {has('mcp') && <td className={NUM_CELL}>{r.mcp != null ? fmtNumber(r.mcp) : '—'}</td>}
                  {has('cpuCores') && (
                    <td className={NUM_CELL}>
                      {r.cpuCores != null
                        ? fmtNumber(r.cpuCores, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                        : '—'}
                    </td>
                  )}
                  {has('uptimeS') && <td className={NUM_CELL}>{fmtUptime(r.uptimeS)}</td>}
                  {/* A spawn that names no agent genuinely has none recorded; an em dash
                      says "unknown" the same way every other column does, where a
                      blank cell reads as a rendering fault. */}
                  <td className="px-3 py-1.5 text-left text-[11.5px] text-accent whitespace-nowrap">
                    {r.agent || <span className="text-muted">—</span>}
                  </td>
                  {/* A pid is an identifier, not a quantity: locale grouping
                      would render 4066648 as "4,066,648" and break copy-paste
                      into ps/kill. */}
                  <td className={NUM_CELL}>{r.pid != null ? String(r.pid) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Activity Monitor's footer: the load trace answers "is this getting
          worse", which no single instantaneous row can, and the stat grid holds
          the host-scale numbers that would otherwise force a second card. */}
      <div className="mt-3 pt-3 border-t border-border grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto_auto]">
        <div>
          <div className="flex items-baseline gap-2 mb-1">
            <span className="text-[11.5px] text-muted">{i18nT('pages.sessionMemoryCard.memory_load')}</span>
            <span className="text-[12.5px] font-mono tabular-nums text-accent">
              {totals?.host_pct != null
                ? fmtPercent(totals.host_pct / 100, { maximumFractionDigits: 1 })
                : '—'}
            </span>
          </div>
          {bars.length > 0 ? (
            <div className="h-12 flex items-end gap-px" aria-hidden="true">
              {/* min-h keeps a real but tiny share visible instead of rounding it
                  away to nothing; the height itself stays the true percentage. */}
              {bars.map((pct, i) => (
                <div
                  key={i}
                  className="flex-1 bg-accent/60 rounded-t-[1px] min-h-[2px]"
                  style={{ height: `${pct}%` }}
                />
              ))}
            </div>
          ) : (
            <div className="h-12 flex items-center text-[11.5px] text-muted">
              {i18nT('pages.sessionMemoryCard.collecting_samples')}
            </div>
          )}
        </div>
        <div className="grid gap-1 content-start">
          <Stat label={i18nT('pages.sessionMemoryCard.physical_memory')} value={fmtGb(hostMb)} />
          <Stat label={i18nT('pages.sessionMemoryCard.kirocrew_used')} value={fmtGb(usedMb)} />
          <Stat
            label={i18nT('pages.sessionMemoryCard.host_headroom')}
            value={hostMb ? fmtGb(hostMb - usedMb) : '—'}
          />
          <Stat label={i18nT('pages.sessionMemoryCard.largest_session')} value={fmtGb(largestMb)} />
        </div>
        <div className="grid gap-1 content-start">
          <Stat label={i18nT('pages.sessionMemoryCard.processes')} value={fmtNumber(procTotal)} />
          <Stat label={i18nT('pages.sessionMemoryCard.mcp_stubs')} value={fmtNumber(mcpTotal)} />
          <Stat label={i18nT('pages.sessionMemoryCard.sessions')} value={fmtNumber(sessions.length)} />
          <Stat label={i18nT('pages.sessionMemoryCard.tasks_running')} value={fmtNumber(tasks.length)} />
        </div>
      </div>
    </Card>
  )
}
