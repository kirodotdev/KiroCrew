// Debug-only React commit timing.
//
// Answers "which part of the UI is re-rendering, how often, and how expensively"
// using React's own <Profiler>, without a devtools session attached.
//
// OFF by default and off in every normal load. It arms only when explicitly
// asked for, via `?profile=commits` in the URL or a `kirocrew.profileCommits`
// localStorage key. When disarmed, `withCommitProfiler` returns its children
// untouched, so React sees no extra element and there is no wrapper in the tree.
//
// IMPORTANT LIMITATION, stated up front because it changes how to read the
// numbers: <Profiler> only reports real timings in a build of react-dom that
// includes profiling instrumentation. In the production bundle the callback is
// effectively inert, so this is a `npm run dev` tool. It is not a way to measure
// shipped performance on a user's machine, and the report says so at runtime
// rather than quietly showing zeros.
import { Profiler, type ReactNode } from 'react'

const URL_FLAG = 'profile'
const URL_VALUE = 'commits'
const STORAGE_KEY = 'kirocrew.profileCommits'

// Bounded so a long session cannot grow this without limit; commits are frequent.
const MAX_RECORDS = 2000

export interface CommitRecord {
  id: string
  phase: 'mount' | 'update' | 'nested-update'
  /** Time spent rendering the committed update, in ms. */
  actualDuration: number
  /** Estimated time to render the whole subtree without memoization, in ms. */
  baseDuration: number
  commitTime: number
}

export interface CommitSummaryRow {
  id: string
  commits: number
  mounts: number
  updates: number
  totalMs: number
  maxMs: number
  meanMs: number
  /** Sum of baseDuration - actualDuration: roughly what memoization saved. */
  savedMs: number
}

const records: CommitRecord[] = []

/** Whether commit profiling has been explicitly armed for this page load. */
export function commitProfilingEnabled(
  search: string = typeof window === 'undefined' ? '' : window.location.search,
  storage: Pick<Storage, 'getItem'> | null = typeof window === 'undefined'
    ? null
    : safeLocalStorage()
): boolean {
  try {
    const params = new URLSearchParams(search || '')
    if (params.get(URL_FLAG) === URL_VALUE) return true
  } catch {
    // A malformed query string must not break app startup.
  }
  try {
    return storage?.getItem(STORAGE_KEY) === '1'
  } catch {
    // Storage access throws in some privacy modes; treat as disabled.
    return false
  }
}

function safeLocalStorage(): Pick<Storage, 'getItem'> | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function recordCommit(record: CommitRecord): void {
  records.push(record)
  // Drop oldest first: the recent window is what an operator is looking at.
  if (records.length > MAX_RECORDS) records.splice(0, records.length - MAX_RECORDS)
}

export function clearCommits(): void {
  records.length = 0
}

export function getCommits(): readonly CommitRecord[] {
  return records
}

/** Aggregate the raw records per Profiler id, heaviest total first. */
export function summarizeCommits(source: readonly CommitRecord[] = records): CommitSummaryRow[] {
  const byId = new Map<string, CommitSummaryRow>()
  for (const r of source) {
    if (!r || typeof r.id !== 'string') continue
    const actual = Number.isFinite(r.actualDuration) ? r.actualDuration : 0
    const base = Number.isFinite(r.baseDuration) ? r.baseDuration : 0
    const row =
      byId.get(r.id) ??
      { id: r.id, commits: 0, mounts: 0, updates: 0, totalMs: 0, maxMs: 0, meanMs: 0, savedMs: 0 }
    row.commits += 1
    if (r.phase === 'mount') row.mounts += 1
    else row.updates += 1
    row.totalMs += actual
    row.maxMs = Math.max(row.maxMs, actual)
    row.savedMs += Math.max(0, base - actual)
    byId.set(r.id, row)
  }
  const rows = [...byId.values()]
  for (const row of rows) row.meanMs = row.commits ? row.totalMs / row.commits : 0
  // Byte comparison, not localeCompare: these ids are machine identifiers passed
  // to <Profiler id=...>, and a locale-sensitive tiebreak would make a diagnostic
  // table order differently on different machines for no benefit.
  rows.sort((a, b) => b.totalMs - a.totalMs || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  return rows
}

/** Render the summary as fixed-width text for the console. */
export function renderCommitSummary(rows: CommitSummaryRow[] = summarizeCommits()): string {
  if (!rows.length) return 'No commits recorded. Arm with ?profile=commits and interact with the UI.'
  const lines: string[] = []
  lines.push(`${'TOTAL'.padStart(9)} ${'MAX'.padStart(8)} ${'MEAN'.padStart(8)} ${'N'.padStart(5)} ${'SAVED'.padStart(9)}  ID`)
  for (const r of rows) {
    lines.push(
      `${r.totalMs.toFixed(1).padStart(9)} ${r.maxMs.toFixed(1).padStart(8)} ` +
        `${r.meanMs.toFixed(2).padStart(8)} ${String(r.commits).padStart(5)} ` +
        `${r.savedMs.toFixed(1).padStart(9)}  ${r.id}`
    )
  }
  lines.push('')
  lines.push('TOTAL/MAX/MEAN/SAVED are milliseconds. SAVED is baseDuration minus')
  lines.push('actualDuration summed -- roughly what memoization avoided.')
  return lines.join('\n')
}

/**
 * Wrap `children` in a <Profiler> when armed, otherwise return them unchanged.
 *
 * Returning children untouched (rather than always mounting a Profiler with a
 * no-op callback) means the disarmed path adds nothing to the element tree.
 */
export function withCommitProfiler(id: string, children: ReactNode): ReactNode {
  if (!commitProfilingEnabled()) return children
  return (
    <Profiler
      id={id}
      onRender={(profilerId, phase, actualDuration, baseDuration, _startTime, commitTime) => {
        recordCommit({
          id: profilerId,
          phase: phase as CommitRecord['phase'],
          actualDuration,
          baseDuration,
          commitTime,
        })
      }}
    >
      {children}
    </Profiler>
  )
}

/**
 * Expose the read helpers on `window` so they can be called from the console
 * without a devtools extension. Only runs when armed.
 */
export function installCommitProfilerConsoleApi(): void {
  if (typeof window === 'undefined' || !commitProfilingEnabled()) return
  const api = {
    summary: () => renderCommitSummary(),
    rows: () => summarizeCommits(),
    raw: () => getCommits(),
    clear: () => clearCommits(),
  }
  ;(window as unknown as Record<string, unknown>).kirocrewCommits = api
  // eslint-disable-next-line no-console
  console.info(
    'Commit profiling armed. Call kirocrewCommits.summary() for a table.\n' +
      'Note: React only reports real timings in a profiling-enabled build of ' +
      'react-dom, so use this with `npm run dev`; production numbers are inert.'
  )
}
