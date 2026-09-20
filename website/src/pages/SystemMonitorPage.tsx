/**
 * SystemMonitorPage — the Chat Resource Monitor, a task-manager style view of
 * live per-chat / per-subagent / gateway resource usage plus host headroom.
 *
 * The snapshot is served pre-sampled and cached by the gateway
 * (`GET /api/system/chat-resources`), so the page can poll cheaply. Polling is
 * driven by an explicit interval that PAUSES while the document is hidden and is
 * torn down on unmount — a background tab must not keep the gateway sampling for
 * a view nobody is looking at.
 *
 * Operators can stop a runaway chat or dedicated-subagent runtime directly from
 * its row: a confirmation dialog guards the request, which reuses the existing
 * session-stop / subagent-cancel routes — the monitor never force-kills. The
 * stopped entry simply drops out (or changes state) on the next poll, so no
 * optimistic list surgery or full reload is needed; a failed stop surfaces
 * inline on the row and leaves it in place.
 *
 * Scope note: surface/route registration lives in a later task.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Badge, Btn, EmptyState } from '../components/ui'
import { Activity } from 'lucide-react'
import InfoTip from '../components/InfoTip'
import { useConfirm } from '../components/ConfirmDialog'
import ErrorNotice from '../components/ErrorNotice'
import SortableHeader from '../components/SortableHeader'
import { useSortableTable, type Comparators } from '../hooks/useSortableTable'
import { fmtBytes, fmtDuration, fmtNumber, fmtPercent } from '../i18n/format'
import { i18nT } from '../i18n/t'

/** One attributed process tree in the snapshot. Fields mirror the backend
 *  `EntrySample` dataclass (snake_case); a `null` numeric is "unavailable" and
 *  renders as an em dash, never as zero. */
export interface MonitorEntry {
  kind: 'chat' | 'subagent' | 'worker' | 'gateway'
  session_key: string
  label: string
  agent: string
  pid: number
  proc_count: number
  rss_mb: number | null
  cpu_pct: number | null
  uptime_s: number | null
  slot: string
  subagent_id: string
  /** The tree reached the sampler's pid bound; rss/cpu/proc_count are a lower bound. */
  truncated: boolean
  /** The sampled runtime's per-spawn instance id ("" when it has none). Sent
   *  with a Stop alongside `pid`, because the OS reuses pids: a replacement
   *  runtime can inherit this row's pid but never its instance id. */
  instance: string
  /** A stop is in flight on this chat's slot (soft cancel pending or hard kill
   *  running). A second stop request in that window ESCALATES to the kill and
   *  drops the slot's queued prompts, so the row's Stop stays locked while true. */
  stop_pending: boolean
}

/** The full monitor response. Mirrors the backend `ResourceSnapshot` dataclass. */
export interface MonitorSnapshot {
  entries: MonitorEntry[]
  posture: string
  available_gb: number
  host_total_gb: number | null
  cpu_count: number | null
  cgroup_used_gb: number | null
  cgroup_limit_gb: number | null
  sampling_supported: boolean
  captured_at: number
  interval_s: number
}

/** Poll cadence for the monitor. The gateway caches within its own sampling
 *  window, so a request inside that window is served from cache rather than
 *  re-walking process trees. */
const POLL_INTERVAL_MS = 3000

/** Query key for the snapshot, following the `['system', ...]` family the other
 *  host-level reads use. */
const MONITOR_QUERY_KEY = ['system', 'chat-resources'] as const

/** Share-of-budget thresholds above which a row is flagged a top consumer.
 *  A tree holding more than a quarter of the memory budget, or more than half a
 *  core, is the thing an operator opened this page to find. */
const RSS_SHARE_WARN = 0.25
const CPU_CORE_WARN = 50 // percent of a single core

const errMsg = (e: unknown): string => (e instanceof Error && e.message ? e.message : String(e))

/** Map a posture string to a Badge variant. `unknown`/anything else stays muted
 *  rather than alarming — an unmeasured posture is not a critical one. */
function postureVariant(posture: string): 'ok' | 'warn' | 'err' | 'muted' {
  switch (posture) {
    case 'ample':
      return 'ok'
    case 'tight':
      return 'warn'
    case 'critical':
      return 'err'
    default:
      return 'muted'
  }
}

/** A dash for an unavailable (null) figure; otherwise the formatted value. */
function memoryCell(rssMb: number | null): string {
  if (rssMb === null) return '—'
  return fmtBytes(rssMb * 1000 * 1000, { maximumFractionDigits: 0 })
}

/** A truncated row's figures cover only the pids the sampler read (its tree
 *  passed the pid bound), so they are lower bounds and must not read as exact:
 *  prefix the "at least" sign. A dash (no reading) stays a dash. */
function lowerBound(text: string, truncated: boolean): string {
  if (!truncated || text === '—') return text
  return `≥${text}`
}

function cpuCell(cpuPct: number | null): string {
  if (cpuPct === null) return '—'
  return fmtPercent(cpuPct / 100, { maximumFractionDigits: 1 })
}

/** Whole-second granularity is enough for a process uptime; a compact two-part
 *  duration reads faster than a raw second count at the top of the list. Units
 *  and separators go through `fmtDuration` so they localise (de comma-joins,
 *  zh joins with nothing). Above an hour the seconds place is dropped; below it
 *  the leading zero unit is dropped so a young runtime reads `42s`, not `0m 42s`. */
function uptimeCell(uptimeS: number | null): string {
  if (uptimeS === null) return '—'
  const total = Math.max(0, Math.floor(uptimeS))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return fmtDuration([[h, 'hour'], [m, 'minute']], { maximumFractionDigits: 0 })
  return fmtDuration([[m, 'minute'], [s, 'second']], { maximumFractionDigits: 0, dropZero: true })
}

/** The memory budget a per-entry RSS is judged against: the cgroup limit when
 *  the gateway runs under one, else the host total. `null` when neither is
 *  known — in which case no memory-based highlight can fire. */
function memoryBudgetGb(snap: MonitorSnapshot): number | null {
  if (snap.cgroup_limit_gb !== null && snap.cgroup_limit_gb > 0) return snap.cgroup_limit_gb
  if (snap.host_total_gb !== null && snap.host_total_gb > 0) return snap.host_total_gb
  return null
}

/** A row is a top consumer when it holds a warning share of the memory budget OR
 *  burns more than half a core. Either condition alone is enough. */
export function isTopConsumer(entry: MonitorEntry, snap: MonitorSnapshot): boolean {
  const budgetGb = memoryBudgetGb(snap)
  if (budgetGb !== null && entry.rss_mb !== null) {
    const entryGb = entry.rss_mb / 1000
    if (entryGb / budgetGb > RSS_SHARE_WARN) return true
  }
  if (entry.cpu_pct !== null && entry.cpu_pct > CPU_CORE_WARN) return true
  return false
}

/** Sort comparators for `useSortableTable`, one per numeric column. Each compares
 *  ASCENDING (the hook flips the sign for `desc`); a `null` figure is treated as
 *  the lowest value, so under the default descending sort it lands last -- an
 *  "unknown" reading must not outrank a measured small value. Module-level so the
 *  hook's `useMemo` sees a stable reference. */
const MONITOR_COMPARATORS: Comparators<MonitorEntry> = {
  memory: (a, b) => (a.rss_mb ?? -Infinity) - (b.rss_mb ?? -Infinity),
  cpu: (a, b) => (a.cpu_pct ?? -Infinity) - (b.cpu_pct ?? -Infinity),
  uptime: (a, b) => (a.uptime_s ?? -Infinity) - (b.uptime_s ?? -Infinity),
}

/** The biggest consumer belongs at the top, so every column opens descending
 *  and the memory column is the default. */
const MONITOR_DEFAULT_SORT = { key: 'memory', dir: 'desc' as const }
const MONITOR_INITIAL_DIRS = { memory: 'desc', cpu: 'desc', uptime: 'desc' } as const

/** Plain (non-sortable) header cell classes -- byte-identical to the `<th>` the
 *  shared `SortableHeader` renders, so sortable and plain headers sit on one
 *  baseline. Same pairing HooksPage uses. */
const TH_CLS =
  'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium break-keep'

function kindLabel(kind: MonitorEntry['kind']): string {
  switch (kind) {
    case 'chat':
      return i18nT('monitor.kind_chat')
    case 'subagent':
      return i18nT('monitor.kind_subagent')
    case 'gateway':
      return i18nT('monitor.kind_gateway')
    default:
      return i18nT('monitor.kind_worker')
  }
}

/** A stable identity for a row across polls, used both as the React key and to
 *  key per-row stop error state so an error stays pinned to its entry rather
 *  than to a list position that shifts as the snapshot re-sorts. */
function entryKey(entry: MonitorEntry): string {
  return `${entry.kind}:${entry.pid}:${entry.subagent_id || entry.session_key}`
}

/** Only the two runtimes the operator can address have a stop path: a chat slot
 *  (session-stop route) and a dedicated subagent (cancel route). A gateway or
 *  shared worker tree has no single-target stop, so those rows carry no button
 *  rather than one that would fail. */
function stopTarget(entry: MonitorEntry): 'chat' | 'subagent' | null {
  if (entry.kind === 'chat' && entry.slot) return 'chat'
  if (entry.kind === 'subagent' && entry.subagent_id) return 'subagent'
  return null
}

export default function SystemMonitorPage() {
  const queryClient = useQueryClient()
  // Snapshot polling is React Query's job: `refetchInterval` re-polls while the
  // document is visible and pauses in the background (`refetchIntervalInBackground`
  // stays at its default `false`, gated on the focus manager's visibility read);
  // `refetchOnWindowFocus` brings the first frame after re-focus current. An
  // in-flight poll that resolves after a newer one has landed is discarded by the
  // query itself, so no request sequencing is needed here. The gateway caches
  // within its own sampling window, so a 3s poll never re-walks process trees.
  const snapQ = useQuery<MonitorSnapshot, Error>({
    queryKey: MONITOR_QUERY_KEY,
    queryFn: () => api.chatResources(),
    refetchInterval: POLL_INTERVAL_MS,
  })
  const snap = snapQ.data ?? null
  // A failed refetch leaves the last good snapshot in the cache; the error is
  // surfaced INLINE beside it (below), never silently hidden behind stale rows.
  const error = snapQ.error ? errMsg(snapQ.error) : ''
  // Sort state lives in the shared table hook (persisted per table id like every
  // other sortable table in the dashboard); rows are memoised so a re-render
  // without a new snapshot does not re-sort.
  const entries = useMemo(() => snap?.entries ?? [], [snap])
  const { sorted: rows, sort, toggle: toggleSort } = useSortableTable(
    entries,
    'system-monitor',
    MONITOR_COMPARATORS,
    MONITOR_DEFAULT_SORT,
    { initialDirs: MONITOR_INITIAL_DIRS, bidirectional: true },
  )
  // Per-row stop failures, keyed by entryKey so an error stays with its entry
  // as the list re-sorts. A row that stops successfully simply drops out on the
  // next poll, so there is no success state to hold here. Kept as maps (rather
  // than the mutation's single `error`/`isPending`) because several rows can be
  // stopped in quick succession and each must report its own outcome.
  const [stopErrors, setStopErrors] = useState<Record<string, string>>({})
  // Rows with an in-flight stop request, keyed by entryKey -- used to disable the
  // button so a double click cannot fire two stops.
  const [stopping, setStopping] = useState<Record<string, boolean>>({})
  const { confirm, confirmDialog } = useConfirm()

  const stopMutation = useMutation({
    mutationFn: async (entry: MonitorEntry) => {
      const res =
        stopTarget(entry) === 'chat'
          ? // By the runtime this ROW sampled, not by slot alone: the confirm
            // dialog can sit open past the slot moving on (conversation reset,
            // new turn on a new runtime), and the backend refuses (409
            // `stale_row`) rather than aborting the replacement's work.
            await api.stopChatSlotIfPid(entry.slot, entry.pid, entry.instance)
          : await api.spawnCancel(entry.subagent_id)
      // Both stop routes can REFUSE with HTTP 200 -- e.g. a remote-bound chat
      // whose crew is unreachable answers `{ok:false, error, code}`. That is a
      // failed stop, not a success: throw so the row's ErrorNotice shows it and
      // the snapshot is not refetched as if the runtime had gone.
      const refusal = res as { ok?: unknown; error?: unknown } | null | undefined
      if (refusal && refusal.ok === false) {
        throw new Error(
          typeof refusal.error === 'string' && refusal.error
            ? refusal.error
            : i18nT('monitor.stop_refused'),
        )
      }
      return res
    },
    onMutate: (entry) => {
      const key = entryKey(entry)
      setStopping((prev) => ({ ...prev, [key]: true }))
      // Clear any earlier failure for this row before the retry.
      setStopErrors((prev) => {
        if (!(key in prev)) return prev
        const next = { ...prev }
        delete next[key]
        return next
      })
    },
    onSuccess: () => {
      // The entry drops out (or changes state) on the next poll. Invalidate now
      // so the change shows without waiting a full interval -- no optimistic
      // list surgery, no full reload. The row's lock is NOT released here: the
      // backend escalates a second stop on a slot whose soft cancel is still
      // pending to a hard kill (and drops its queued prompts), so the lock is
      // held until a snapshot reports the slot idle again (see the effect below).
      void queryClient.invalidateQueries({ queryKey: MONITOR_QUERY_KEY })
    },
    onError: (e, entry) => {
      setStopErrors((prev) => ({ ...prev, [entryKey(entry)]: errMsg(e) }))
      // A refused stop changed nothing on the backend: unlock so it can be retried.
      setStopping((prev) => {
        const next = { ...prev }
        delete next[entryKey(entry)]
        return next
      })
      // A 409 means the row no longer describes what the slot is running:
      // refetch now so the operator sees the current conversation before
      // deciding again, rather than a row up to one interval stale.
      if ((e as { status?: unknown } | null)?.status === 409) {
        void queryClient.invalidateQueries({ queryKey: MONITOR_QUERY_KEY })
      }
    },
  })

  // Release a row's stop lock only once the backend says the stop has resolved.
  // A chat row unlocks when its snapshot row reports no pending stop (the slot
  // is idle again, so a new press is a fresh soft cancel, not an escalation) or
  // the row is gone; the first refetch after a success sees `stop_pending: true`
  // (the slot records it before the stop route answers), so the lock cannot slip
  // between the response and that poll. A subagent row has no such state and
  // its process is reaped after the cancel, so it unlocks when the row is gone.
  useEffect(() => {
    if (!snap) return
    setStopping((prev) => {
      const keys = Object.keys(prev)
      if (keys.length === 0) return prev
      const present = new Map(snap.entries.map((e) => [entryKey(e), e] as const))
      const next: Record<string, boolean> = {}
      for (const k of keys) {
        const e = present.get(k)
        if (e && (e.kind !== 'chat' || e.stop_pending)) next[k] = true
      }
      return Object.keys(next).length === keys.length ? prev : next
    })
  }, [snap])

  const onStop = useCallback(
    async (entry: MonitorEntry) => {
      if (!stopTarget(entry)) return
      const name = entry.label || entry.session_key || i18nT('monitor.untitled')
      const ok = await confirm({
        title: i18nT('monitor.stop_confirm_title'),
        body: i18nT('monitor.stop_confirm_body', { name }),
        confirmLabel: i18nT('monitor.stop_confirm_action'),
      })
      if (!ok) return
      stopMutation.mutate(entry)
    },
    [confirm, stopMutation],
  )

  const header = (
    <PageHeader
      title={i18nT('monitor.title')}
      subtitle={i18nT('monitor.subtitle')}
    />
  )

  if (!snap) {
    return (
      <>
        {header}
        <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          {error ? (
            <ErrorNotice message={error} variant="inline" askAgent testId="monitor-error" />
          ) : (
            <div className="text-muted text-[13px]" data-testid="monitor-loading">
              {i18nT('monitor.loading')}
            </div>
          )}
        </div>
      </>
    )
  }

  const budgetGb = memoryBudgetGb(snap)

  return (
    <>
      {header}
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0 flex flex-col gap-4">
        {/* Header strip: posture, available memory, and — only when the gateway
            runs under a readable cgroup — a used/limit bar. */}
        <Card>
          <CardTitle>
            {i18nT('monitor.section_host')} <InfoTip text={i18nT('monitor.section_host_help')} />
          </CardTitle>
          <div className="flex flex-wrap items-center gap-4" data-testid="monitor-header">
            <div className="flex items-center gap-2">
              <span className="text-[13px] text-muted">{i18nT('monitor.posture')}</span>
              <Badge variant={postureVariant(snap.posture)} data-testid="monitor-posture">
                {snap.posture}
              </Badge>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[13px] text-muted">{i18nT('monitor.available_memory')}</span>
              <span className="text-[13px] text-text font-mono tabular-nums" data-testid="monitor-available">
                {snap.available_gb >= 0
                  ? fmtBytes(snap.available_gb * 1000 * 1000 * 1000, { maximumFractionDigits: 1 })
                  : '—'}
              </span>
            </div>
            {snap.cgroup_limit_gb !== null && snap.cgroup_used_gb !== null && (
              <div className="flex items-center gap-2 min-w-[180px]" data-testid="monitor-cgroup">
                <span className="text-[13px] text-muted">{i18nT('monitor.cgroup')}</span>
                <div className="flex-1 h-2 rounded-full bg-bg-elevated overflow-hidden min-w-[80px]">
                  <div
                    className="h-full bg-accent"
                    style={{
                      width: `${
                        snap.cgroup_limit_gb > 0
                          ? Math.min(100, (snap.cgroup_used_gb / snap.cgroup_limit_gb) * 100)
                          : 0
                      }%`,
                    }}
                  />
                </div>
                <span className="text-[13px] text-text font-mono tabular-nums whitespace-nowrap">
                  {snap.cgroup_limit_gb > 0
                    ? fmtPercent(snap.cgroup_used_gb / snap.cgroup_limit_gb, { maximumFractionDigits: 0 })
                    : '—'}
                </span>
              </div>
            )}
          </div>
        </Card>

        {/* A stale-but-present error while a snapshot is already shown surfaces
            inline without blanking the table. */}
        {error && <ErrorNotice message={error} variant="inline" askAgent testId="monitor-error" />}

        <Card>
          <CardTitle>
            {i18nT('monitor.section_runtimes')} <InfoTip text={i18nT('monitor.section_runtimes_help')} />
          </CardTitle>
          {!snap.sampling_supported ? (
            <div className="text-[13px] text-muted leading-relaxed" data-testid="monitor-unavailable">
              {i18nT('monitor.sampling_unavailable')}
            </div>
          ) : rows.length === 0 ? (
            <EmptyState
              icon={<Activity className="lucide-inline" />}
              title={i18nT('monitor.no_entries')}
              subtitle={i18nT('monitor.no_entries_hint')}
              testId="monitor-empty"
            />
          ) : (
            <>
            {/* Horizontal scroller, same treatment as the Hooks and Schedule
                tables: on a narrow viewport the eight nowrap columns scroll
                sideways inside the card so the trailing Stop column stays
                reachable instead of being clipped by the card's overflow. */}
            <div className="overflow-x-auto" data-testid="monitor-table-scroller">
            <table className="w-full text-[13px] border-collapse table-striped" data-testid="monitor-table">
              <thead>
                <tr>
                  <th className={TH_CLS}>{i18nT('monitor.col_kind')}</th>
                  <th className={TH_CLS}>{i18nT('monitor.col_name')}</th>
                  <th className={TH_CLS}>{i18nT('monitor.col_agent')}</th>
                  <SortableHeader label={i18nT('monitor.col_memory')} sortKey="memory" sort={sort} onToggle={toggleSort} className="text-right" />
                  <SortableHeader label={i18nT('monitor.col_cpu')} sortKey="cpu" sort={sort} onToggle={toggleSort} className="text-right" />
                  <th className={`${TH_CLS} text-right`}>{i18nT('monitor.col_procs')}</th>
                  <SortableHeader label={i18nT('monitor.col_uptime')} sortKey="uptime" sort={sort} onToggle={toggleSort} className="text-right" />
                  <th className={`${TH_CLS} text-right`}>{i18nT('monitor.col_actions')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((entry) => {
                  const top = isTopConsumer(entry, snap)
                  const key = entryKey(entry)
                  const target = stopTarget(entry)
                  const rowStopError = stopErrors[key]
                  const name =
                    entry.kind === 'chat' && entry.slot ? (
                      <Link to={`/chat?sid=${encodeURIComponent(entry.slot)}`} className="text-accent hover:underline">
                        {entry.label || entry.session_key || i18nT('monitor.untitled')}
                      </Link>
                    ) : (
                      <span className="text-text">{entry.label || i18nT('monitor.untitled')}</span>
                    )
                  return (
                    <tr
                      key={key}
                      data-testid="monitor-row"
                      data-top-consumer={top ? 'true' : 'false'}
                      // Theme variables only -- the warning accent tint comes from
                      // the token, never a fixed colour. Inline (not a utility
                      // class) because `.table-striped tbody tr:nth-child(even)`
                      // outranks a single class and would erase the tint on every
                      // even row.
                      className="border-b border-border last:border-b-0"
                      style={top ? { background: 'var(--warn-subtle)' } : undefined}
                    >
                      <td className="px-3 py-2 whitespace-nowrap text-muted">{kindLabel(entry.kind)}</td>
                      <td className="px-3 py-2 max-w-[280px] truncate">
                        {name}
                        {/* A failed stop stays pinned to its row and does not
                            blank the table; the entry remains in place. */}
                        {rowStopError && (
                          <div className="mt-1 flex">
                            <ErrorNotice
                              message={rowStopError}
                              variant="inline"
                              askAgent
                              testId="monitor-row-error"
                            />
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap text-muted">{entry.agent || '—'}</td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap" data-testid="monitor-memory">
                        {lowerBound(memoryCell(entry.rss_mb), entry.truncated)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap" data-testid="monitor-cpu">
                        {lowerBound(cpuCell(entry.cpu_pct), entry.truncated)}
                      </td>
                      <td
                        className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap"
                        data-testid="monitor-procs"
                        title={entry.truncated ? i18nT('monitor.truncated_hint') : undefined}
                      >
                        {lowerBound(fmtNumber(entry.proc_count), entry.truncated)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                        {uptimeCell(entry.uptime_s)}
                      </td>
                      <td className="px-3 py-2 text-right whitespace-nowrap">
                        {/* Only chat and dedicated-subagent rows carry a stop
                            button; gateway/worker rows have no single-target
                            stop path and render nothing here. */}
                        {target ? (
                          <Btn
                            danger
                            onClick={() => void onStop(entry)}
                            disabled={!!stopping[key] || entry.stop_pending}
                            data-testid="monitor-stop"
                          >
                            {i18nT('monitor.stop')}
                          </Btn>
                        ) : null}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            </div>
            </>
          )}
        </Card>
        {budgetGb === null && snap.sampling_supported && (
          <div className="text-[12px] text-muted" data-testid="monitor-no-budget">
            {i18nT('monitor.no_memory_budget')}
          </div>
        )}
      </div>
      {confirmDialog}
    </>
  )
}
