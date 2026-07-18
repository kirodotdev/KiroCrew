import { safeSetItem } from '../utils/safeStorage'
import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import Clickable from '../components/Clickable'
import { AnimatePresence, motion } from 'framer-motion'
import { List, CalendarDays, ClipboardList, ChevronRight, Globe, Check, History, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Btn, SendBtn, Badge, SearchInput, EmptyState, Skeleton } from '../components/ui'
import SegmentedControl from '../components/SegmentedControl'
import WeekGrid from '../components/WeekGrid'
import TimezoneSelect from '../components/TimezoneSelect'
import JobForm from '../components/JobForm'
import JobLogsView from '../components/JobLogsView'
import type { KiroCrewAgent } from '../components/AgentSelector'
import InfoTip from '../components/InfoTip'
import type { CronJob } from '../types'
import { useAgents } from '../hooks/useAgents'
import { useCronActions } from '../hooks/useCronActions'
import { useAppSelector } from '../store'
import { SaveCreateLabel } from '../utils/cronUtils'
import { useSortableTable } from '../hooks/useSortableTable'
import SortableHeader from '../components/SortableHeader'
import ExecutionsView from '../components/ExecutionsView'
import { sanitizeLlmOutput } from '../utils/sanitize'

const RENDER_TZ_STORAGE_KEY = 'kirocrew.schedule.renderTz'
/**
 * Collapsed-by-default message cell. Shows a 1-line preview with a chevron;
 * click to toggle a <pre> block that preserves whitespace/indentation.
 * Accepts pre-sanitized message to avoid double sanitization (parent memoizes).
 */
export function CollapsibleMessage({ message }: { message: string }) {
  const [open, setOpen] = useState(false)
  const safe = useMemo(() => sanitizeLlmOutput(message), [message])
  const preview = safe.length > 80 ? safe.slice(0, 80).replace(/\s+/g, ' ') + '…' : safe.replace(/\s+/g, ' ')
  return (
    <div className="text-sm">
      <Btn
        onClick={e => { e.stopPropagation(); setOpen(v => !v) }}
        className="!p-0 !border-none !rounded-none flex items-start gap-1 text-left w-full hover:text-text-strong"
        title={open ? 'Collapse' : 'Expand'}
      >
        <ChevronRight size={14} className={`mt-[3px] shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span className={open ? 'text-muted text-[12px] min-w-0' : 'truncate min-w-0'}>{open ? 'Hide message' : preview}</span>
      </Btn>
      {open && (
        // Presentational content block; the handler only stops the click from
        // bubbling to the parent row toggle — it adds no interactive behavior.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/click-events-have-key-events
        <pre
          onClick={e => e.stopPropagation()}
          className="mt-1.5 p-2.5 bg-bg-elevated border border-border rounded-md text-[12px] font-mono whitespace-pre-wrap break-words max-h-[280px] overflow-y-auto leading-relaxed"
        >{safe}</pre>
      )}
    </div>
  )
}


const fmtAgo = (ts?: number) => {
  if (!ts) return '—'
  const s = Math.floor((Date.now() / 1000) - ts)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

const fmtIn = (ts?: number | null) => {
  if (ts == null) return '—'
  const s = Math.floor(ts - Date.now() / 1000)
  if (s <= 0) return 'now'
  if (s < 60) return 'in <1m'
  if (s < 3600) return `in ${Math.floor(s / 60)}m`
  if (s < 86400) { const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); return `in ${h}h ${m}m` }
  const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); return `in ${d}d ${h}h`
}

export default function SchedulePage() {
  const [jobs, setJobs] = useState<CronJob[]>([])
  const { agents, defaultAgent } = useAgents(0)
  const [cronFilter, setCronFilter] = useState('')
  const [selected, setSelected] = useState<CronJob | null>(null)
  const [creating, setCreating] = useState(false)
  const [jobsView, setJobsView] = useState<'list' | 'calendar' | 'executions'>('list')
  const [renderTz, setRenderTz] = useState<string>(() => {
    try {
      const stored = localStorage.getItem(RENDER_TZ_STORAGE_KEY)
      if (stored) return stored
    } catch {
      // localStorage unavailable (private mode) — fall through to default
    }
    return Intl.DateTimeFormat().resolvedOptions().timeZone
  })
  useEffect(() => {
    try {
      safeSetItem(RENDER_TZ_STORAGE_KEY, renderTz)
    } catch {
      // localStorage unavailable — don't block rendering
    }
  }, [renderTz])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  // Batch selection + AWS-style bulk delete
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [batchConfirm, setBatchConfirm] = useState(false)
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [batchError, setBatchError] = useState<string | null>(null)
  const [confirmText, setConfirmText] = useState('')
  const sanitizedJobs = useMemo(() => jobs.map(j => ({ ...j, safeMessage: sanitizeLlmOutput(j.message) })), [jobs])

  const load = useCallback(async () => {
    try {
      setLoadError(null)
      const d = await api.crons()
      const fresh: CronJob[] = d.jobs || []
      setJobs(fresh)
      setSelected(prev => prev ? fresh.find((j: CronJob) => j.id === prev.id) ?? null : null)
      // Drop any selected IDs that no longer exist (deleted elsewhere / by us).
      setSelectedIds(prev => {
        if (prev.size === 0) return prev
        const live = new Set(fresh.map(j => j.id))
        const next = new Set([...prev].filter(id => live.has(id)))
        return next.size === prev.size ? prev : next
      })
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Failed to load jobs')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => { load() }, [load])

  // Auto-reload when backend pushes a 'crons' refresh (e.g. job starts/ends).
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  useEffect(() => { if (refreshTrigger > 0) load() }, [refreshTrigger, load])

  const { running, actionError, setActionError, runNow, openInChat } = useCronActions(load)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const confirmRevertTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const armDelete = useCallback((id: string) => {
    setConfirmDeleteId(id)
    if (confirmRevertTimer.current) clearTimeout(confirmRevertTimer.current)
    confirmRevertTimer.current = setTimeout(() => setConfirmDeleteId(null), 3000)
  }, [])
  useEffect(() => () => { if (confirmRevertTimer.current) clearTimeout(confirmRevertTimer.current) }, [])
  const deleteJob = useCallback(async (id: string) => {
    try {
      if (confirmRevertTimer.current) clearTimeout(confirmRevertTimer.current)
      setDeletingId(id)
      await api.deleteCron(id)
      setSelected(prev => prev?.id === id ? null : prev)
      await load()
    } catch (e: unknown) {
      setActionError({ id, msg: e instanceof Error ? e.message : 'Delete failed' })
    } finally {
      setDeletingId(null)
      setConfirmDeleteId(null)
    }
  }, [load, setActionError])
  const filteredJobs = useMemo(() => sanitizedJobs.filter(j => !cronFilter || (j.name+' '+j.safeMessage+' '+(j.agent||'')+' '+(j.model||'')).toLowerCase().includes(cronFilter.toLowerCase())), [sanitizedJobs, cronFilter])
  const scheduleComparators = useMemo(() => ({
    name: (a: CronJob, b: CronJob) => a.name.localeCompare(b.name),
    schedule: (a: CronJob, b: CronJob) => (a.schedule || '').localeCompare(b.schedule || ''),
    status: (a: CronJob, b: CronJob) => {
      const rank = (j: CronJob) =>
        j.is_running ? 4 : !j.enabled ? 0 : j.last_status === 'error' ? 1 : j.last_status === 'ok' ? 2 : 3;
      return rank(a) - rank(b);
    },
    lastRun: (a: CronJob, b: CronJob) => (a.last_run_ts || 0) - (b.last_run_ts || 0),
    nextRun: (a: CronJob, b: CronJob) => (a.next_run_ts || 0) - (b.next_run_ts || 0),
  }), [])
  const { sorted: sortedScheduleJobs, sort: schedSort, toggle: toggleSchedSort } = useSortableTable(filteredJobs, 'cron-schedule', scheduleComparators, { key: 'nextRun', dir: 'asc' })

  // ── Batch selection helpers (operate over the currently visible/filtered rows) ──
  const allVisibleSelected = sortedScheduleJobs.length > 0 && sortedScheduleJobs.every(j => selectedIds.has(j.id))
  const someVisibleSelected = sortedScheduleJobs.some(j => selectedIds.has(j.id))
  const toggleOne = useCallback((id: string) => {
    setSelectedIds(prev => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n })
  }, [])
  const toggleAllVisible = useCallback(() => {
    setSelectedIds(prev => {
      const allSel = sortedScheduleJobs.length > 0 && sortedScheduleJobs.every(j => prev.has(j.id))
      const n = new Set(prev)
      if (allSel) sortedScheduleJobs.forEach(j => n.delete(j.id))
      else sortedScheduleJobs.forEach(j => n.add(j.id))
      return n
    })
  }, [sortedScheduleJobs])
  const clearSelection = useCallback(() => setSelectedIds(new Set()), [])
  const selectedJobs = useMemo(() => jobs.filter(j => selectedIds.has(j.id)), [jobs, selectedIds])
  const openBatchConfirm = useCallback(() => { setBatchError(null); setConfirmText(''); setBatchConfirm(true) }, [])
  const runBatchDelete = useCallback(async () => {
    const ids = Array.from(selectedIds)
    if (ids.length === 0) return
    setBatchDeleting(true); setBatchError(null)
    try {
      const res = await api.batchDeleteCron(ids)
      const failed: string[] = Array.isArray(res?.failed) ? res.failed : []
      setSelected(prev => prev && selectedIds.has(prev.id) && !failed.includes(prev.id) ? null : prev)
      await load()
      if (failed.length) {
        // Keep the failures selected so the user can retry; surface the count.
        setSelectedIds(new Set(failed))
        setBatchError(`${failed.length} of ${ids.length} job${ids.length === 1 ? '' : 's'} could not be deleted`)
      } else {
        setSelectedIds(new Set())
        setBatchConfirm(false)
      }
    } catch (e) {
      setBatchError(e instanceof Error ? e.message : 'Batch delete failed')
    } finally {
      setBatchDeleting(false)
    }
  }, [selectedIds, load])
  const confirmArmed = confirmText.trim().toLowerCase() === 'delete'

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        <PageHeader title="Schedule" subtitle="Manage recurring cron jobs and scheduled tasks" />
        <div className="flex-1 overflow-y-auto px-6 pb-8 min-h-0">
          {loadError ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <p className="text-danger text-sm mb-3">{loadError}</p>
              <Btn onClick={load}>Retry</Btn>
            </div>
          ) : loading ? (
            <div className="flex items-center justify-center py-20"><Skeleton className="h-6 w-32 rounded" /></div>
          ) : jobs.length === 0 && !creating ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <svg className="w-16 h-16 stroke-current fill-none text-muted/20 mb-4" viewBox="0 0 24 24" strokeWidth={1} strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                <line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>
                <line x1="3" y1="10" x2="21" y2="10"/>
                <circle cx="12" cy="15" r="1.5"/>
                <path d="M9.5 15h-2M16.5 15h-2"/>
              </svg>
              <div className="text-muted text-sm font-medium">No scheduled jobs yet</div>
              <p className="text-sm text-muted max-w-[360px] mb-5 mt-2">Schedule recurring tasks to run automatically — check pipelines, generate reports, monitor services, or anything your agent can do.</p>
              <SendBtn onClick={() => { setSelected(null); setCreating(true) }}>
                <span className="flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                  Create your first job
                </span>
              </SendBtn>
              <p className="text-[12px] text-muted mt-3">or <a href="/chat" className="text-accent hover:underline">ask in chat</a> — try "remind me to check my pipeline every morning"</p>
            </div>
          ) : (<>
          <div className="flex items-center gap-2 px-3 py-2.5 mb-4 rounded-lg bg-accent-subtle border border-accent/20 text-[13px] text-text">
            <svg className="w-4 h-4 stroke-current fill-none shrink-0 text-accent" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <span>You can also create schedules by chatting — try <em>"remind me to check my pipeline every morning at 9am"</em></span>
            <a href="/chat" className="ml-auto text-accent text-[13px] font-medium shrink-0 hover:underline">Open Chat</a>
          </div>

          <Card><CardTitle>
            <div className="flex items-center justify-between w-full">
              <span className="flex items-center gap-1.5">Jobs <InfoTip text="Scheduled jobs run on the configured interval or cron expression." /></span>
              <div className="flex items-center gap-2">
                <SendBtn onClick={() => { setSelected(null); setCreating(true) }}>
                  <span className="flex items-center gap-1.5">
                    <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                    Add Job
                  </span>
                </SendBtn>
                <SegmentedControl
                  segments={[
                    { key: 'list' as const, label: 'List', icon: <List size={14} /> },
                    { key: 'calendar' as const, label: 'Calendar', icon: <CalendarDays size={14} /> },
                    { key: 'executions' as const, label: 'Executions', icon: <History size={14} /> },
                  ]}
                  value={jobsView}
                  onChange={setJobsView}
                  layoutId="schedule-view"
                />
              </div>
            </div>
          </CardTitle>
            {jobsView === 'calendar' ? (<>
              <div className="flex items-center gap-2 mb-3 text-[13px] text-muted">
                <Globe className="lucide-inline" />
                {/* Control is correctly associated via htmlFor+id (the select can't be nested); label-has-for's nesting requirement is a false positive here. */}
                {/* eslint-disable-next-line jsx-a11y/label-has-for */}
                <label htmlFor="schedule-render-tz" className="mr-1">Render in</label>
                <TimezoneSelect id="schedule-render-tz" value={renderTz} onChange={setRenderTz} />
                <InfoTip text="Changes only how the calendar grid is displayed — does not change when any job actually fires." />
              </div>
              <WeekGrid jobs={jobs} selectedId={selected?.id} onSelect={setSelected} renderTz={renderTz} />
            </>) : jobsView === 'executions' ? (
              <ExecutionsView selectedJobId={selected?.id} />
            ) : (<>
            <div className="mb-3 flex items-center gap-2">
              <div className="flex-1 min-w-0"><SearchInput placeholder="Filter jobs…" value={cronFilter} onChange={e => setCronFilter(e.target.value)} /></div>
              {selectedIds.size > 0 && (
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[13px] text-muted whitespace-nowrap">{selectedIds.size} selected</span>
                  <Btn onClick={clearSelection}>Clear</Btn>
                  <Btn danger onClick={openBatchConfirm} title={`Delete ${selectedIds.size} selected job(s)`}>
                    <span className="flex items-center gap-1.5"><Trash2 size={14} /> Delete {selectedIds.size} selected</span>
                  </Btn>
                </div>
              )}
            </div>
            <div className="overflow-x-auto"><table className="w-full border-collapse table-striped"><thead><tr>
              <th className="px-2.5 py-2 border-b border-border w-[36px] text-center">
                <input
                  type="checkbox"
                  aria-label="Select all jobs"
                  title="Select / deselect all jobs matching the current filter"
                  className="accent-accent cursor-pointer align-middle"
                  checked={allVisibleSelected}
                  ref={el => { if (el) el.indeterminate = !allVisibleSelected && someVisibleSelected }}
                  onChange={toggleAllVisible}
                />
              </th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[72px]">ID</th>
              <SortableHeader label="Name" sortKey="name" sort={schedSort} onToggle={toggleSchedSort} className="w-[100px]" />
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[80px]">Type</th>
              <SortableHeader label="Schedule" sortKey="schedule" sort={schedSort} onToggle={toggleSchedSort} className="w-[110px]" />
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[200px]">Message</th>
              <SortableHeader label="Status" sortKey="status" sort={schedSort} onToggle={toggleSchedSort} className="w-[70px]" />
              <SortableHeader label="Last Run" sortKey="lastRun" sort={schedSort} onToggle={toggleSchedSort} className="w-[80px]" />
              <SortableHeader label="Next Run" sortKey="nextRun" sort={schedSort} onToggle={toggleSchedSort} className="w-[90px]" />
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[210px]">Actions</th>
            </tr></thead>
            <tbody>{jobs.length === 0
              ? <tr><td colSpan={10}><EmptyState icon={<ClipboardList className="lucide-inline" />} title="No cron jobs" /></td></tr>
              : sortedScheduleJobs.length === 0
              ? <tr><td colSpan={10} className="text-muted italic px-2.5 py-3.5 text-sm">No matching jobs</td></tr>
              : sortedScheduleJobs.map(j => (
              <tr key={j.id} className={`hover:bg-bg-hover transition-colors cursor-pointer ${selected?.id === j.id ? 'bg-accent-subtle' : ''} ${selectedIds.has(j.id) ? 'bg-accent-subtle/60' : ''}`} onClick={() => { setCreating(false); setSelected(selected?.id === j.id ? null : j) }}>
                <td className="px-2.5 py-2 border-b border-border text-center" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    aria-label={`Select ${j.name}`}
                    className="accent-accent cursor-pointer align-middle"
                    checked={selectedIds.has(j.id)}
                    onChange={() => toggleOne(j.id)}
                  />
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><code>{j.id}</code></td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{j.name}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{j.script ? <span className="text-[var(--accent)] font-medium text-[13px]">script · python</span> : j.command ? <span className="text-[var(--warn)] font-medium text-[13px]">command · shell</span> : <span className="text-muted text-[13px]">agent · {j.agent || 'default'}{j.model ? ` · ${j.model}` : ''}</span>}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm"><code>{j.schedule}</code>{j.timezone && <span className="block text-[11px] text-muted">{j.timezone.replace(/_/g, ' ')}</span>}</td>
                <td className="px-2.5 py-2 border-b border-border align-top max-w-[360px]"><CollapsibleMessage message={j.script ? j.script : j.command ? j.command : j.safeMessage} /></td>
                <td className="px-2.5 py-2 border-b border-border text-sm" title={j.last_error || j.last_result || ''}>{j.is_running ? <Badge variant="ok"><span className="inline-block w-1.5 h-1.5 rounded-full bg-ok animate-pulse mr-1" />Running</Badge> : j.enabled ? (j.last_status === 'ok' ? <Badge variant="ok">OK</Badge> : j.last_status === 'error' ? <Badge variant="err">Error</Badge> : <Badge variant="ok">Ready</Badge>) : <Badge variant="warn">Paused</Badge>}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{fmtAgo(j.last_run_ts)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm text-muted" title={j.next_run_ts ? new Date(j.next_run_ts * 1000).toLocaleString() : ''}>{fmtIn(j.next_run_ts)}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm whitespace-nowrap" onClick={e => e.stopPropagation()}>
                  <span title={j.strict_schedule ? 'Disable strict schedule (allow jitter)' : 'Enable strict schedule (no jitter)'}><Btn onClick={async () => { try { await api.updateCron(j.id, { strict_schedule: !j.strict_schedule }); load() } catch (e: unknown) { setActionError({ id: j.id, msg: e instanceof Error ? e.message : 'Failed' }) } }}>{j.strict_schedule ? <><Check className="lucide-inline" /> Strict</> : 'Strict'}</Btn></span>{' '}
                  <span title={j.enabled ? 'Run now' : 'Resume to run'}><Btn onClick={() => runNow(j.id)} disabled={!j.enabled || running.has(j.id)}>{running.has(j.id) ? '...' : 'Run'}</Btn></span>{' '}
                  <span title={j.has_slot ? 'Continue session' : j.has_result ? 'View last result' : 'No result'}><Btn onClick={() => openInChat(j.id)} disabled={!j.has_result && !j.has_slot}>{j.has_slot ? 'Continue' : 'View'}</Btn></span>{' '}
                  <Btn onClick={async () => { try { await api.toggleCron(j.id, !j.enabled); load() } catch (e: unknown) { setActionError({ id: j.id, msg: e instanceof Error ? e.message : 'Failed' }) } }}>{j.enabled ? 'Pause' : 'Resume'}</Btn>{' '}
                  <Btn
                    danger
                    disabled={deletingId === j.id}
                    title={confirmDeleteId === j.id ? 'Click again to confirm' : 'Delete job'}
                    onClick={() => confirmDeleteId === j.id ? deleteJob(j.id) : armDelete(j.id)}
                  >{deletingId === j.id ? '...' : confirmDeleteId === j.id ? 'Confirm' : 'Delete'}</Btn>
                  {actionError?.id === j.id && <span className="text-danger text-[12px] ml-1">{actionError.msg}</span>}
                </td>
              </tr>
            ))}</tbody></table></div>
            </>)}
          </Card>
          </>)}
        </div>
      </div>

      <AnimatePresence>
        {(selected || creating) && (
          <motion.div
            key="panel"
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 'auto', opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="shrink-0 overflow-hidden h-full"
          >
            <JobDetailPanel
              key={selected?.id || 'new'}
              job={selected || undefined}
              agents={agents}
              defaultAgent={defaultAgent}
              onClose={() => { setSelected(null); setCreating(false) }}
              onSaved={() => { load(); setSelected(null); setCreating(false) }}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {batchConfirm && (
        <Clickable
          className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100]"
          onClick={() => { if (!batchDeleting) setBatchConfirm(false) }}
        >
          {/* Modal container; handlers only stop backdrop-dismiss from firing — a dialog role is non-interactive to jsx-a11y but these guards are idiomatic for a modal. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Delete ${selectedIds.size} scheduled jobs`}
            className="bg-bg-elevated rounded-xl border border-border p-6 w-[460px] max-w-[92vw] shadow-xl animate-scale-in"
            onClick={e => e.stopPropagation()}
            onKeyDown={e => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-text mb-2 flex items-center gap-2">
              <Trash2 size={16} className="text-danger shrink-0" />
              Delete {selectedIds.size} scheduled job{selectedIds.size === 1 ? '' : 's'}?
            </h3>
            <p className="text-sm text-muted mb-3">This permanently removes the selected job{selectedIds.size === 1 ? '' : 's'} and their run history. This action cannot be undone.</p>
            <div className="max-h-[168px] overflow-y-auto rounded-md border border-border bg-bg divide-y divide-border/60 mb-4">
              {selectedJobs.map(jb => (
                <div key={jb.id} className="flex items-center gap-2 px-3 py-1.5 text-[13px]">
                  <code className="text-muted shrink-0">{jb.id}</code>
                  <span className="truncate text-text">{jb.name}</span>
                </div>
              ))}
            </div>
            <label htmlFor="batch-delete-confirm" className="block text-[13px] text-muted mb-1.5">
              Type <code className="text-text font-semibold">delete</code> to confirm
            </label>
            <input
              id="batch-delete-confirm"
              autoFocus
              value={confirmText}
              onChange={e => setConfirmText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && confirmArmed && !batchDeleting) runBatchDelete() }}
              placeholder="delete"
              className="w-full mb-4 px-3 py-2 rounded-md bg-bg border border-border text-sm text-text outline-none focus:border-accent"
            />
            <div className="flex gap-2 justify-end">
              <Btn onClick={() => setBatchConfirm(false)} disabled={batchDeleting}>Cancel</Btn>
              <Btn danger disabled={batchDeleting || !confirmArmed} onClick={runBatchDelete}>
                {batchDeleting ? 'Deleting…' : `Delete ${selectedIds.size}`}
              </Btn>
            </div>
            {batchError && <p className="text-danger text-[12px] mt-2">{batchError}</p>}
          </div>
        </Clickable>
      )}
    </div>
  )
}

// Room to keep clear for the job-list column so this panel can't grow past its
// flex row and reflow content off-screen (mirrors DetailPanel's reserveWidth).
const JOB_LIST_MIN = 360

function JobDetailPanel({ job, agents, defaultAgent, onClose, onSaved }: {
  job?: CronJob; agents: KiroCrewAgent[]; defaultAgent: string; onClose: () => void; onSaved: () => void
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [panelError, setPanelError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [width, setWidth] = useState(380)
  const [, setDragging] = useState(false)
  const [detailTab, setDetailTab] = useState<'details' | 'logs'>('details')
  useEffect(() => { setDetailTab('details') }, [job?.id])
  const panelRef = useRef<HTMLDivElement>(null)
  const submitRef = useRef<(() => void) | null>(null)
  const moveRef = useRef<((ev: MouseEvent) => void) | null>(null)
  const upRef = useRef<(() => void) | null>(null)
  const widthRef = useRef(width)
  widthRef.current = width

  useEffect(() => {
    return () => {
      if (moveRef.current) document.removeEventListener('mousemove', moveRef.current)
      if (upRef.current) document.removeEventListener('mouseup', upRef.current)
    }
  }, [])

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setDragging(true)
    const startX = e.clientX; const startW = widthRef.current
    const onMove = (ev: MouseEvent) => {
      // Cap to the panel's room in its flex row (row width minus the job-list
      // minimum), not a fraction of the whole window: the panel is `shrink-0`
      // in an `overflow-hidden` row, so a window-based cap lets it overflow the
      // row and reflow content off-screen. Expected ancestor chain: panelRef div
      // -> wrapping motion.div -> the flex row; if that nesting changes the
      // optional chain silently falls back to the viewport (restoring the old
      // over-cap), so keep the two levels in sync with the render tree below.
      // Unlike DetailPanel this only re-caps during drag (width is ephemeral,
      // not persisted, and there's no mount/resize re-clamp here) — a
      // pre-existing gap left as-is to keep this fix scoped.
      const rowW = panelRef.current?.parentElement?.parentElement?.getBoundingClientRect().width ?? window.innerWidth
      const cap = Math.min(rowW - JOB_LIST_MIN, Math.round(window.innerWidth * 0.6))
      setWidth(Math.max(300, Math.min(startW + (startX - ev.clientX), cap)))
    }
    const onUp = () => {
      setDragging(false)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      moveRef.current = null; upRef.current = null
    }
    moveRef.current = onMove; upRef.current = onUp
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [])

  return (
    <div ref={panelRef} className="shrink-0 border-l border-border bg-bg flex flex-col h-full overflow-hidden relative" style={{ width, minWidth: 300 }}>
      {/* Resize splitter: role=separator is the correct semantic, but jsx-a11y treats it as non-interactive; the mousedown drag is intrinsic to a resize handle. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div role="separator" aria-orientation="vertical" aria-label="Resize panel" className="absolute left-[-2px] top-0 bottom-0 w-[5px] cursor-col-resize z-20 group/drag flex items-center justify-center" onMouseDown={onDragStart}>
        <div className="w-[2px] h-full bg-transparent group-hover/drag:bg-accent group-active/drag:bg-accent-hover transition-colors duration-200" />
      </div>
      <div className="flex items-center justify-between px-5 py-4 border-b border-border">
        <span className="text-base font-semibold text-text-strong truncate">{job ? job.name : 'New Job'}</span>
        <Btn aria-label="Close" onClick={onClose}>
          <svg className="w-4 h-4 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </Btn>
      </div>
      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-4">
        {job && (
          <div className="flex items-center justify-between">
            <SegmentedControl
              segments={[
                { key: 'details' as const, label: 'Details' },
                { key: 'logs' as const, label: 'Logs' },
              ]}
              value={detailTab}
              onChange={setDetailTab}
              layoutId="panel-tab"
            />
            <div className="flex gap-2">
              <Btn onClick={async () => { try { await api.toggleCron(job.id, !job.enabled); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : 'Failed') } }}>{job.enabled ? 'Pause' : 'Resume'}</Btn>
              <SendBtn onClick={async () => { try { await api.runCron(job.id); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : 'Failed') } }}>Run Now</SendBtn>
            </div>
          </div>
        )}
        {!job && (
          <div className="flex items-center justify-between">
            <Badge variant="ok">New</Badge>
          </div>
        )}
        {detailTab === 'logs' && job ? (
          <JobLogsView jobId={job.id} isRunning={job.is_running} runningSince={job.running_since} />
        ) : (
          <>
            <JobForm job={job} agents={agents} defaultAgent={defaultAgent} onSaved={onSaved} layout="vertical" externalSubmit submitRef={submitRef} onSavingChange={setSaving} />
            {panelError && <div className="text-danger text-[13px]">{panelError}</div>}
            {job?.script && (job.last_result || job.last_error) && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[12px] text-muted font-medium">{job.last_error ? 'Last Error' : 'Last Output'}</div>
                <pre className={`text-[12px] font-mono whitespace-pre-wrap break-words rounded border px-2.5 py-2 max-h-[200px] overflow-y-auto ${job.last_error ? 'bg-danger/5 border-danger/20 text-danger' : 'bg-bg-elevated border-border text-text'}`}>{job.last_error || job.last_result}</pre>
              </div>
            )}
            {job?.last_run_ts && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[12px] text-muted font-medium">Last Run</div>
                <span className="text-sm text-text">{new Date(job.last_run_ts * 1000).toLocaleString()}</span>
              </div>
            )}
          </>
        )}
      </div>
      {/* Fixed footer */}
      <div className="shrink-0 px-5 py-3 border-t border-border flex items-center justify-between">
        {job ? (
          <Btn danger onClick={() => setConfirmDelete(true)}>
            <span className="flex items-center gap-1.5">
              <svg className="w-3.5 h-3.5 stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              Delete
            </span>
          </Btn>
        ) : <div />}
        <SendBtn onClick={() => submitRef.current?.()} disabled={saving}>
          <SaveCreateLabel isEdit={!!job} saving={saving} />
        </SendBtn>
      </div>
      {confirmDelete && job && (
        <Clickable className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100]" onClick={() => setConfirmDelete(false)}>
          {/* Modal container; handlers only stop backdrop-dismiss from firing — a dialog role is non-interactive to jsx-a11y but these guards are idiomatic for a modal. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Delete ${job.name}`}
            className="bg-bg-elevated rounded-xl border border-border p-6 w-[360px] max-w-[90vw] shadow-xl animate-scale-in"
            onClick={e => e.stopPropagation()}
            onKeyDown={e => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-text mb-2">Delete &quot;{job.name}&quot;?</h3>
            <p className="text-sm text-muted mb-4">This will permanently remove the scheduled job. This action cannot be undone.</p>
            <div className="flex gap-2 justify-end">
              <Btn onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn danger disabled={deleting} onClick={async () => { try { setDeleteError(null); setDeleting(true); await api.deleteCron(job.id); onSaved() } catch (e: unknown) { setDeleteError(e instanceof Error ? e.message : 'Delete failed') } finally { setDeleting(false) } }}>{deleting ? 'Deleting...' : 'Delete'}</Btn>
            </div>
            {deleteError && <p className="text-danger text-[12px] mt-2">{deleteError}</p>}
          </div>
        </Clickable>
      )}
    </div>
  )
}
