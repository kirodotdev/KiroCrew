/**
 * Row model for the Sessions table — the pure half, with no React in it.
 *
 * The atom is a SESSION. Everything that consumes resources on behalf of a user
 * is one: dashboard chat, Slack, Discord, an agent cron, and a subagent task. A
 * task is not a different kind of thing, it is a session that has a parent, so
 * it nests one level via `subRows` and that edge survives every grouping choice.
 *
 * Sorting, filtering, grouping, aggregation and expansion all belong to
 * `@tanstack/react-table`, which is why this module builds a tree and stops.
 * The comparator, the collapsed-key set and the per-view column map that used to
 * live here were a hand-rolled reimplementation of exactly those row models.
 */
import { api } from '../../api/client'
import { fmtDuration, fmtNumber, fmtPercent, fmtUnit, type FormatUnit } from '../../i18n/format'

type Payload = Awaited<ReturnType<typeof api.sessionsMemory>>
export type SessionPayloadRow = Payload['sessions'][number]
export type TaskPayloadRow = Payload['tasks'][number]

/** One table row. Tasks hang off their session in `subRows`. */
export interface SessionRow {
  kind: 'session' | 'task'
  id: string
  name: string
  agent: string
  channel: string
  rssMb: number | null
  peakMb: number | null
  cpuCores: number | null
  procs: number | null
  mcp: number | null
  credits: number | null
  turns: number | null
  uptimeS: number | null
  pid: number | null
  /** Runtime is multiplexed, so this row's numbers are an attributed share. */
  shared: boolean
  /** Route to the row's chat window, or null when it has none to open. */
  href: string | null
  subRows?: SessionRow[]
}

/**
 * Route that opens a session's chat window, or null when there is none.
 *
 * Only dashboard sessions have one. `_bg`, cron and Slack sessions are real
 * sessions with real memory but nothing to navigate to; returning null keeps
 * them in the table as non-interactive rows rather than shipping a click that
 * silently does nothing.
 *
 * ChatPage resolves the session from `?sid=` and dispatches `switchSlot` itself,
 * so navigation alone suffices. The param takes the BARE slot key: the
 * `dashboard:` prefix belongs to the backend session key.
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
 * Host-scale totals as a bare number of GB. Per-row cells stay in MB so the
 * column is directly comparable, but host totals in MB read as six digits.
 *
 * Deliberately unit-LESS: the unit belongs in the label's catalog string.
 * `fmtUnit(x, 'gigabyte')` emits a literal "GB" that no catalog owns, so under
 * the pseudolocale it renders as untranslated Latin glued to its label — which
 * is what the i18n render gate flags as latin-leak/untranslated-text.
 */
export function fmtGb(mb: number | null | undefined): string {
  if (mb == null || !Number.isFinite(mb)) return '—'
  return fmtNumber(mb / 1024, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** Share of host RAM. Takes MB on both sides and hands the RATIO to Intl. */
export function fmtHostPct(mb: number | null | undefined, hostMb: number | null): string {
  if (mb == null || !hostMb) return '—'
  return fmtPercent(mb / hostMb, { maximumFractionDigits: 2 })
}

/** Cumulative credits as a 2dp number; null (not measured) renders as em dash. */
export function fmtCredits(credits: number | null | undefined): string {
  if (credits == null || !Number.isFinite(credits)) return '—'
  return fmtNumber(credits, { minimumFractionDigits: 1, maximumFractionDigits: 1 })
}

/** Turn count; null (not measured) renders as em dash. */
export function fmtTurns(turns: number | null | undefined): string {
  if (turns == null) return '—'
  return fmtNumber(turns)
}

/**
 * Uptime as a compound duration. Coarse on purpose: once a session is days old
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
 * History samples -> one bar height per sample, as a percentage of the host
 * total.
 *
 * The y-axis is scaled to the host total rather than the series max, so a bar's
 * height means "share of this machine" instead of "share of its own peak" — a
 * self-scaled trace makes 200 MB of churn look like a crisis. A flat row of
 * short bars is the correct picture of an idle machine.
 */
export function sparklineBars(
  history: Array<{ t: number; mb: number }>,
  hostMb: number | null,
): number[] {
  if (history.length < 2) return []
  const ceiling = hostMb && hostMb > 0 ? hostMb : Math.max(...history.map(h => h.mb)) || 1
  return history.map(h => Math.min(100, Math.max(0, (h.mb / ceiling) * 100)))
}

/**
 * A session with no generated title yet must still be distinguishable — every
 * such row would otherwise read identically — so the slot key disambiguates it.
 */
export function rowName(row: SessionPayloadRow): string {
  if (row.untitled && row.slot_key) return `${row.title} ${row.slot_key}`
  return row.title || row.key
}

/** Cell heat, as a fraction of the largest value in the column. */
export function heatLevel(value: number | null, max: number | null): 0 | 1 | 2 | 3 {
  if (value == null || !max || max <= 0) return 0
  const share = value / max
  if (share >= 0.66) return 3
  if (share >= 0.33) return 2
  if (share >= 0.1) return 1
  return 0
}

function taskRow(t: TaskPayloadRow): SessionRow {
  return {
    kind: 'task',
    id: t.id,
    name: t.task,
    agent: t.agent,
    // A task inherits nothing about where it came from: it was spawned by an
    // agent, not by a user on a channel. Grouping by channel therefore folds it
    // under its own bucket rather than misreporting its parent's origin.
    channel: 'subagent',
    rssMb: t.sampled ? t.rss_mb : null,
    peakMb: t.sampled ? t.peak_rss_mb : null,
    cpuCores: t.sampled ? t.cpu_cores : null,
    procs: null,
    mcp: null,
    credits: null,
    turns: null,
    uptimeS: t.started_at ? Date.now() / 1000 - t.started_at : null,
    pid: t.pid,
    shared: t.shared,
    href: null,
  }
}

/**
 * Sessions + tasks -> the tree TanStack Table consumes.
 *
 * Order is not decided here: the table owns sorting, so imposing one would make
 * the first paint disagree with every subsequent one.
 *
 * A task whose `parent` matches no session is emitted as a TOP-LEVEL row rather
 * than dropped. Dropping it is the contradiction this page exists to remove: the
 * footer counts `tasks.length`, so an unmatched task would be counted in
 * "Task sessions" and be absent from the table above it. An orphan happens for
 * real — an app-spawned task can carry an empty parent key, and a task can
 * outlive the session that spawned it — and it is still a live runtime the
 * reader may need to act on.
 */
export function buildTree(sessions: SessionPayloadRow[], tasks: TaskPayloadRow[]): SessionRow[] {
  const sessionKeys = new Set(sessions.map(s => s.key))
  const rows = sessions.map(s => {
    const mine = tasks.filter(t => t.parent === s.key).map(taskRow)
    return {
      kind: 'session' as const,
      id: s.key,
      name: rowName(s),
      agent: s.agent,
      channel: s.channel,
      rssMb: s.rss_mb,
      peakMb: null,
      cpuCores: s.cpu_cores,
      procs: s.procs,
      mcp: s.mcp,
      credits: s.credits ?? null,
      turns: s.turns ?? null,
      uptimeS: s.uptime_s,
      pid: s.pid,
      shared: !s.owns_runtime,
      href: sessionChatPath(s.key),
      ...(mine.length > 0 ? { subRows: mine } : {}),
    }
  })
  const orphans = tasks.filter(t => !sessionKeys.has(t.parent)).map(taskRow)
  return orphans.length > 0 ? [...rows, ...orphans] : rows
}

/** The largest value per numeric column, for the heat tint. Tasks included. */
export function columnMaxima(rows: SessionRow[]): { rssMb: number | null; cpuCores: number | null } {
  let rssMb: number | null = null
  let cpuCores: number | null = null
  const visit = (r: SessionRow): void => {
    if (r.rssMb != null && (rssMb == null || r.rssMb > rssMb)) rssMb = r.rssMb
    if (r.cpuCores != null && (cpuCores == null || r.cpuCores > cpuCores)) cpuCores = r.cpuCores
    for (const c of r.subRows ?? []) visit(c)
  }
  for (const r of rows) visit(r)
  return { rssMb, cpuCores }
}
