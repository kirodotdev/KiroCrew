import { safeSetItem } from '../utils/safeStorage'
import { useState, useEffect, useCallback, useRef, useMemo, Fragment } from 'react'
import { useImeGuard } from '../hooks/useImeGuard'
import Clickable from '../components/Clickable'
import { List, CalendarDays, CalendarClock, Plus, ClipboardList, ChevronRight, Globe, History, Trash2, FolderPlus, MoreHorizontal, Pencil, Folder, LayoutGrid, GitPullRequestArrow, Download, KeyRound } from 'lucide-react'
import { api } from '../api/client'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useArmedDelete } from '../hooks/useArmedDelete'
import { PageHeader, Card, Btn, SendBtn, Badge, SearchInput, EmptyState, FilteredEmpty, Skeleton, Input } from '../components/ui'
import { CodeBlock } from '../components/CodeBlock'
import SegmentedControl from '../components/SegmentedControl'
import WeekGrid from '../components/WeekGrid'
import TimezoneSelect from '../components/TimezoneSelect'
import JobForm from '../components/JobForm'
import JobLogsView from '../components/JobLogsView'
import ErrorNotice from '../components/ErrorNotice'
import type { KiroCrewAgent } from '../components/AgentSelector'
import InfoTip from '../components/InfoTip'
import type { CronJob } from '../types'
import { useAgents } from '../hooks/useAgents'
import { useCronActions } from '../hooks/useCronActions'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { useAppSelector, useAppDispatch } from '../store'
import { triggerRefresh } from '../store/dashboardSlice'
import { SaveCreateLabel, scheduleLabel, scheduleMinutes } from '../utils/cronUtils'
import { useSortableTable } from '../hooks/useSortableTable'
import { SortableTableHead } from '../components/SortableHeader'
import ExecutionsView from '../components/ExecutionsView'
import { sanitizeLlmOutput } from '../utils/sanitize'
import { SCHEDULE_PRESETS, type CronPrefill, type SchedulePreset } from '../utils/schedulePresets'
import { groupJobsByFolder, loadCollapsedFolders, saveCollapsedFolders } from '../utils/cronFolders'
import type { CronFolder } from '../utils/cronFolders'
import CronFolderHeader from '../components/CronFolderHeader'
import CronJobMoveMenu from '../components/CronJobMoveMenu'
import CronRowActions from '../components/CronRowActions'
import AddJobSplitButton from '../components/AddJobSplitButton'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../components/ui/dropdown-menu'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '../components/ui/table'
import {
  Dialog, DialogContent, DialogHeader, DialogBody, DialogFooter, DialogTitle, DialogDescription,
} from '../components/ui/dialog'
import ScheduleTemplateGallery from '../components/ScheduleTemplateGallery'

import { i18nT } from '../i18n/t'
import { defaultAgentQuery } from '../api/defaultAgentQuery'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { compareText, fmtDateTimeNumeric } from '../i18n/format'
import { formatCadence } from '../utils/scheduleCadence'
const RENDER_TZ_STORAGE_KEY = 'kirocrew.schedule.renderTz'

/**
 * Column count of the jobs table — the `colSpan` every full-width row uses
 * (empty states, folder headers, the ungrouped divider, per-folder errors).
 *
 * One constant rather than a literal per row: the folder rows were passing 11
 * against a 10-column table, which a browser tolerates but which silently rots
 * the moment a column is added or removed.
 */
const SCHEDULE_COLUMNS = 10

/**
 * Literal token the user must type to arm bulk delete.
 *
 * NOT display copy and NOT translatable: it is compared verbatim against the
 * input (`confirmArmed`), so a translated token can never satisfy the check —
 * the button stays disabled and bulk delete becomes unreachable in that
 * language. A zh-CN user typing the displayed 删除 hit exactly that.
 *
 * Exported so the instruction, the placeholder, and the comparison all read the
 * SAME value: they cannot drift, and there is no bare string literal here for
 * the i18n codemod to convert on a future run.
 */
export const BULK_DELETE_TOKEN = 'delete'
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
        title={open ? i18nT('pages.schedulePage.collapse') : i18nT('pages.schedulePage.expand')}
      >
        <ChevronRight size={14} className={`mt-[3px] shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
        <span className={open ? 'text-muted text-[12px] min-w-0' : 'truncate min-w-0'}>{open ? i18nT('pages.schedulePage.hide_message') : preview}</span>
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
  if (s < 60) return i18nT('pages.schedulePage.just_now')
  if (s < 3600) return i18nT('pages.schedulePage.m_ago', { n: Math.floor(s / 60) })
  if (s < 86400) return i18nT('pages.schedulePage.h_ago', { n: Math.floor(s / 3600) })
  return i18nT('pages.schedulePage.d_ago', { n: Math.floor(s / 86400) })
}

const fmtIn = (ts?: number | null) => {
  if (ts == null) return '—'
  const s = Math.floor(ts - Date.now() / 1000)
  if (s <= 0) return i18nT('pages.schedulePage.now')
  // `in <1m` deliberately stays English for now: as a NEW catalog value it is
  // rejected by check-source-strings' `leading-connector` rule, which cannot
  // separate a lowercase standalone label from a real sentence fragment (its
  // own comment names `in progress` as the same known limit). The `${}` branches
  // below need a key plus `{{vars}}`, which is Phase 6.
  if (s < 60) return 'in <1m'
  if (s < 3600) return `in ${Math.floor(s / 60)}m`
  if (s < 86400) { const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); return `in ${h}h ${m}m` }
  const d = Math.floor(s / 86400); const h = Math.floor((s % 86400) / 3600); return `in ${d}d ${h}h`
}

/**
 * Empty-state folder chip with confirm-before-delete, inline rename, and error display.
 * Uses the same inline-edit pattern as CronFolderHeader (Enter=commit, Escape=cancel).
 */
function EmptyFolderChip({ folder, onRename, onDelete, error }: { folder: CronFolder; onRename: (name: string) => void; onDelete: () => void; error: string | null }) {
  const [confirming, setConfirming] = useState(false)
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState(folder.name)
  const ime = useImeGuard()

  const commitRename = () => {
    const trimmed = editName.trim()
    if (trimmed && trimmed !== folder.name) onRename(trimmed)
    setEditing(false)
  }

  return (
    <div>
      <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-bg-elevated/30 border border-border mb-1.5">
        <Folder size={14} className="text-accent shrink-0" />
        {editing ? (
          <Input
            autoFocus
            aria-label={i18nT('pages.schedulePage.cronFolders.rename')}
            className="bg-bg rounded px-2 py-0.5 flex-none min-w-[120px]"
            value={editName}
            onChange={e => setEditName(e.target.value)}
            {...ime.bindEnter({
              onEnter: commitRename,
              onEscape: () => setEditing(false),
              onBlur: commitRename,
            })}
          />
        ) : (
          <span className="text-sm font-medium text-text">{folder.name}</span>
        )}
        <span className="text-[12px] text-muted">{i18nT('pages.schedulePage.cronFolders.job_count', { count: 0 })}</span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Btn className="!p-1 !border-none ml-auto" aria-label={i18nT('pages.schedulePage.cronFolders.folder_actions')}>
              <MoreHorizontal size={14} />
            </Btn>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[140px]">
            <DropdownMenuItem onSelect={() => { setEditName(folder.name); setTimeout(() => setEditing(true), 0) }}>
              <Pencil size={13} className="shrink-0" />
              <span>{i18nT('pages.schedulePage.cronFolders.rename')}</span>
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => setConfirming(true)} className="text-danger">
              <Trash2 size={13} className="shrink-0" />
              <span>{i18nT('pages.schedulePage.cronFolders.delete_folder')}</span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {confirming && (
        <div className="flex items-center gap-3 px-3 py-1.5 mb-1.5 text-sm rounded-md bg-danger/5 border border-danger/20">
          <span className="text-text">{i18nT('pages.schedulePage.cronFolders.confirm_delete_folder', { name: folder.name })}</span>
          <Btn danger onClick={() => { setConfirming(false); onDelete() }}>
            {i18nT('pages.schedulePage.cronFolders.delete_folder_named', { name: folder.name })}
          </Btn>
          <Btn onClick={() => setConfirming(false)}>
            {i18nT('pages.schedulePage.cancel')}
          </Btn>
        </div>
      )}
      {error && (
        <div className="px-3 py-1 mb-1.5">
          <span className="text-danger text-[12px]">{error}</span>
        </div>
      )}
    </div>
  )
}

export default function SchedulePage() {
  const [jobs, setJobs] = useState<CronJob[]>([])
  const dispatch = useAppDispatch()
  const { agents, error: rosterError, reload: reloadRoster, reloading: rosterReloading } = useAgents(0)
  // A recovered roster must not be recovered for this form alone. `useAgents`
  // holds PER-INSTANCE state, and the app shell keeps its own copy (App.tsx
  // feeds it to the agent-cycle shortcuts), so a retry that refreshed only this
  // page would tell the user the roster is back while another surface still
  // holds the empty one. Bumping the shared refresh trigger — the same channel
  // chat already uses after an agent operation — makes one press recover every
  // consumer.
  const recoverRoster = useCallback(() => {
    reloadRoster()
    dispatch(triggerRefresh())
  }, [reloadRoster, dispatch])
  // Paired at the boundary: the picker is handed a failure it can act on, or
  // nothing at all — never an error with no way out of it.
  const rosterFailure = rosterError ? { reloading: rosterReloading, onReload: recoverRoster } : undefined
  // The default agent comes from the shared, WS-invalidated + focus-refetched
  // query rather than useAgents' one-shot value, so the agent-column label's
  // freshness matches the agents rail's — one source of truth (issue #6495).
  const { data: defaultAgentData } = useQuery(defaultAgentQuery)
  const defaultAgent = defaultAgentData ?? ''
  const [cronFilter, setCronFilter] = useState('')
  const [selected, setSelected] = useState<CronJob | null>(null)
  /**
   * Whether the job detail dialog is showing for `selected`.
   *
   * Deliberately NOT folded into `selected`: that state also drives the row
   * highlight, the calendar's selected entry, and the Executions view's job
   * filter, and all three must survive dismissing the dialog. The detail view
   * used to be a side panel that could stay open beside those views; a modal
   * cannot, so conflating "which job is selected" with "the detail view is
   * open" would drop the filter the moment the user closed the modal to look
   * at what it was filtering.
   */
  const [detailOpen, setDetailOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [prefill, setPrefill] = useState<CronPrefill | null>(null)
  // Whether the seeded preset performs repo/issue writes (shows a notice in the create panel).
  const [prefillWrites, setPrefillWrites] = useState(false)
  // Bumped on every preset pick so re-selecting the same preset remounts the
  // create panel (the panel is keyed on this; without it, edits from a prior
  // pick of the same preset would leak into the "fresh" form).
  const [prefillNonce, setPrefillNonce] = useState(0)
  const [jobsView, setJobsView] = useState<'list' | 'calendar' | 'executions'>('list')
  const [galleryOpen, setGalleryOpen] = useState(false)
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

  // ── Cron Folders ──
  // Folder definitions come through React Query (standard data-fetch path).
  // Failure degrades gracefully: no page-level error, prior data is kept on a
  // failed refetch, and `[]` renders the folderless layout.
  const queryClient = useQueryClient()
  const { data: cronFolders = [] } = useQuery({
    queryKey: ['cronFolders'],
    queryFn: async () => ((await api.cronFolders()) as CronFolder[]) || [],
  })
  const refreshFolders = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ['cronFolders'] }),
    [queryClient],
  )
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(loadCollapsedFolders)
  const [folderModal, setFolderModal] = useState<{ mode: 'create'; resolve?: (id: string | undefined) => void } | null>(null)
  const [folderModalName, setFolderModalName] = useState('')
  const folderNameIme = useImeGuard()
  const batchConfirmIme = useImeGuard()
  const [folderModalError, setFolderModalError] = useState<string | null>(null)
  const toggleFolderCollapse = useCallback((folderId: string) => {
    setCollapsedFolders(prev => {
      const next = new Set(prev)
      if (next.has(folderId)) next.delete(folderId)
      else next.add(folderId)
      saveCollapsedFolders(next)
      return next
    })
  }, [])

  // Monotonic sequence guard: prevents stale load() responses from overwriting newer state.
  const loadSeq = useRef(0)

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    try {
      setLoadError(null)
      // Jobs are primary -- folders failure must not break the page.
      const d = await api.crons()
      if (seq !== loadSeq.current) return // stale response
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
      if (seq !== loadSeq.current) return // stale response
      setLoadError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed_to_load_jobs'))
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [])
  useEffect(() => { load() }, [load])

  // Auto-reload when backend pushes a 'crons' refresh (e.g. job starts/ends,
  // or a run is cancelled) — supersedes interval polling for is_running state.
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  useEffect(() => { if (refreshTrigger > 0) { load(); refreshFolders() } }, [refreshTrigger, load, refreshFolders])

  const { running, actionError, setActionError, runNow, openInChat, cancelling, cancelRun } = useCronActions(load)
  // Measured overflow state for the jobs table's scroller — drives the fade cue
  // at the pinned Actions column's edge. Measured, not breakpoint-inferred: the
  // table overflows whenever the CONTAINER is narrower than its min-width,
  // which a resizable nav rail can cause at any viewport size.
  const [attachJobsScroller, jobsTableEdges] = useScrollEdges<HTMLElement>()
  // Stable wrapper, like the hook's own callback ref: an inline arrow would be
  // a new function every render, and React detaches/reattaches a changed ref —
  // each detach writes edge state, which re-renders, which loops.
  const attachJobsTable = useCallback(
    (el: HTMLTableElement | null) => attachJobsScroller(el?.parentElement ?? null),
    [attachJobsScroller],
  )

  // ── Cron Folder handlers (depend on load) ──
  const handleNewFolder = useCallback(async (moveTo?: boolean): Promise<string | undefined> => {
    return new Promise<string | undefined>((resolve) => {
      setFolderModalName('')
      setFolderModalError(null)
      setFolderModal({ mode: 'create', resolve: moveTo ? resolve : undefined })
      // If not moveTo, resolve immediately (fire-and-forget open modal)
      if (!moveTo) resolve(undefined)
    })
  }, [])
  const handleFolderModalSubmit = useCallback(async () => {
    const name = folderModalName.trim()
    if (!name) return
    try {
      setFolderModalError(null)
      const res = await api.createCronFolder(name) as { id: string }
      await refreshFolders()
      setFolderModal(prev => { prev?.resolve?.(res.id); return null })
    } catch (e) {
      // Keep modal OPEN so user can correct the name — show inline error
      setFolderModalError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed'))
    }
  }, [folderModalName, refreshFolders])
  const handleMoveJob = useCallback(async (jobId: string, folderId: string) => {
    try {
      await api.updateCron(jobId, { folder_id: folderId })
      // Auto-expand the target folder so moved job stays visible
      if (folderId) {
        setCollapsedFolders(prev => {
          if (!prev.has(folderId)) return prev
          const next = new Set(prev)
          next.delete(folderId)
          saveCollapsedFolders(next)
          return next
        })
      }
      await load()
    } catch (e) {
      setActionError({ id: jobId, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.failed') })
    }
  }, [load, setActionError])
  const handleDeleteFolder = useCallback(async (folderId: string) => {
    try {
      await api.deleteCronFolder(folderId)
      setActionError(null)
      await Promise.all([refreshFolders(), load()])
    } catch (e) {
      setActionError({ id: `folder-${folderId}`, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.failed') })
    }
  }, [load, refreshFolders, setActionError])

  // performDelete reports its own errors, so confirmDelete never rejects.
  const performDelete = useCallback(async (id: string) => {
    try {
      await api.deleteCron(id)
      setSelected(prev => prev?.id === id ? null : prev)
      await load()
    } catch (e: unknown) {
      setActionError({ id, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.delete_failed') })
    }
  }, [load, setActionError])
  const { armedId: confirmDeleteId, arm: armDelete, confirm: confirmDelete, isDeleting } = useArmedDelete(performDelete)
  const filteredJobs = useMemo(() => sanitizedJobs.filter(j => !cronFilter || (j.name+' '+j.safeMessage+' '+(j.agent||'')+' '+(j.model||'')+' '+(j.session_key||'')).toLowerCase().includes(cronFilter.toLowerCase())), [sanitizedJobs, cronFilter])
  const scheduleComparators = useMemo(() => ({
    name: (a: CronJob, b: CronJob) => a.name.localeCompare(b.name),
    // Clock time first, so `9:00 AM` precedes `1:00 PM` -- the label sorts wrongly
    // as text. Rows with no clock (raw fallback, intervals) go last, then by label.
    schedule: (a: CronJob, b: CronJob) => {
      const ka = scheduleMinutes(a), kb = scheduleMinutes(b)
      if (ka === null || kb === null) {
        if (ka !== kb) return ka === null ? 1 : -1
        return compareText(scheduleLabel(a), scheduleLabel(b))
      }
      return ka - kb || compareText(scheduleLabel(a), scheduleLabel(b))
    },
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
  // Rows actually visible in the table: jobs inside a collapsed folder render
  // no row (collapse is bypassed while a filter is active), so select-all must
  // not silently include them.
  const visibleScheduleJobs = useMemo(
    () => cronFilter
      ? sortedScheduleJobs
      : sortedScheduleJobs.filter(j => !(j.folder_id && collapsedFolders.has(j.folder_id))),
    [sortedScheduleJobs, cronFilter, collapsedFolders],
  )
  const allVisibleSelected = visibleScheduleJobs.length > 0 && visibleScheduleJobs.every(j => selectedIds.has(j.id))
  const someVisibleSelected = visibleScheduleJobs.some(j => selectedIds.has(j.id))
  const toggleOne = useCallback((id: string) => {
    setSelectedIds(prev => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n })
  }, [])
  const toggleAllVisible = useCallback(() => {
    setSelectedIds(prev => {
      const allSel = visibleScheduleJobs.length > 0 && visibleScheduleJobs.every(j => prev.has(j.id))
      const n = new Set(prev)
      if (allSel) visibleScheduleJobs.forEach(j => n.delete(j.id))
      else visibleScheduleJobs.forEach(j => n.add(j.id))
      return n
    })
  }, [visibleScheduleJobs])
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
        // `count` (the total) drives plural-category selection; `{{failed}}` is
        // interpolation-only. Catalog values must keep the noun agreeing with
        // {{count}}, not {{failed}}.
        setBatchError(i18nT('pages.schedulePage.job_could_not_be_deleted', { count: ids.length, failed: failed.length }))
      } else {
        setSelectedIds(new Set())
        setBatchConfirm(false)
      }
    } catch (e) {
      setBatchError(e instanceof Error ? e.message : i18nT('pages.schedulePage.batch_delete_failed'))
    } finally {
      setBatchDeleting(false)
    }
  }, [selectedIds, load])
  const confirmArmed = confirmText.trim().toLowerCase() === BULK_DELETE_TOKEN

  // Open the create panel blank (from "Create your first job" / "Add Job").
  const openBlankCreate = useCallback(() => { setSelected(null); setDetailOpen(false); setPrefill(null); setCreating(true) }, [])
  // Open the create panel seeded from a pre-canned schedule card.
  const openPreset = useCallback((p: SchedulePreset) => { setSelected(null); setDetailOpen(false); setPrefill(p.prefill); setPrefillWrites(!!p.writes); setPrefillNonce(n => n + 1); setCreating(true) }, [])
  // Open the detail dialog on a job (row click / calendar entry click).
  const openDetail = useCallback((job: CronJob) => { setCreating(false); setPrefill(null); setSelected(job); setDetailOpen(true) }, [])
  // Dismiss the dialog. `selected` survives on purpose — see its declaration.
  const closeDetail = useCallback(() => { setDetailOpen(false); setCreating(false); setPrefill(null); setPrefillWrites(false) }, [])
  // The dialog is bound to `selected` existing, so a job deleted underneath us
  // (its row removed by `load()`) closes the dialog without a second signal.
  const detailDialogOpen = creating || (detailOpen && !!selected)

  // When the templates empty state is showing, use an 8px bottom pad (matching
  // the left-nav panel's m-2 edge) so the card row's bottom lines up with the
  // sidebar's bottom. The list/table view keeps the standard pb-8.
  const showEmptyState = !loading && !loadError && jobs.length === 0 && !creating

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        {/* View switching is NAVIGATION, so it sits at page level rather than in
            the list's own toolbar — next to three action buttons it read as
            three more of them. `collapse={false}`: this lives in the header's
            flex row whose width it contributes to, so the responsive
            measurement would be circular (same reason the Crews page passes
            it). */}
        {/* `subtitle` is ONE key, never a sentence assembled from two: the i18n
            render gate's `fragment/multi-unit` rule fires on a visible text run
            built from several catalog lookups, and rightly — a translator cannot
            reorder across the seam. The chat affordance the deleted banner
            carried moved into the Add Job menu, where it is its own text unit. */}
        <PageHeader
          title={i18nT('pages.schedulePage.schedule')}
          subtitle={i18nT('pages.schedulePage.manage_recurring_cron_jobs_and_scheduled_tasks')}
          actions={
            <SegmentedControl
              segments={[
                { key: 'list' as const, label: i18nT('pages.schedulePage.view_list'), icon: <List size={14} /> },
                { key: 'calendar' as const, label: i18nT('pages.schedulePage.view_calendar'), icon: <CalendarDays size={14} /> },
                { key: 'executions' as const, label: i18nT('pages.schedulePage.view_executions'), icon: <History size={14} /> },
              ]}
              value={jobsView}
              onChange={setJobsView}
              layoutId="schedule-view"
              collapse={false}
            />
          }
        />
        <div className={`flex-1 overflow-y-auto px-3 sm:px-6 min-h-0 ${showEmptyState ? 'pb-2' : 'pb-8'}`}>
          {loadError ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <ErrorNotice message={loadError} askAgent className="mb-3" />
              <Btn onClick={load}>{i18nT('pages.schedulePage.retry')}</Btn>
            </div>
          ) : loading ? (
            <div className="flex items-center justify-center py-20"><Skeleton className="h-6 w-32 rounded" /></div>
          ) : jobs.length === 0 && !creating ? (
            <div className="flex flex-col sm:h-full min-h-0">
              {cronFolders.length > 0 && (
                <div className="mb-4">
                  {cronFolders.map(f => (
                    <EmptyFolderChip
                      key={`empty-fh-${f.id}`}
                      folder={f}
                      onRename={async (name) => { try { await api.updateCronFolder(f.id, { name }); await refreshFolders() } catch (e) { setActionError({ id: `folder-${f.id}`, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.failed') }) } }}
                      onDelete={() => handleDeleteFolder(f.id)}
                      error={actionError?.id === `folder-${f.id}` ? actionError.msg : null}
                    />
                  ))}
                </div>
              )}
              <div className="sm:flex-1 flex flex-col items-center sm:justify-center text-center min-h-0 py-4 sm:py-8">
                <CalendarClock className="w-16 h-16 text-muted/20 mb-4" strokeWidth={1} aria-hidden="true" />
                <div className="text-muted text-sm font-medium">{i18nT('pages.schedulePage.no_scheduled_jobs_yet')}</div>
                <p className="text-sm text-muted max-w-[360px] mb-5 mt-2">{i18nT('pages.schedulePage.schedule_recurring_tasks_to_run_automatically_ch')}</p>
                <SendBtn onClick={openBlankCreate}>
                  <span className="flex items-center gap-1.5">
                    <Plus size={14} aria-hidden="true" />
                    {i18nT('pages.schedulePage.create_your_first_job')}
                  </span>
                </SendBtn>
                <p className="text-[12px] text-muted mt-3">{i18nT('pages.schedulePage.or')} <a href="/chat" className="text-accent hover:underline">{i18nT('pages.schedulePage.ask_in_chat')}</a> {i18nT('pages.schedulePage.try_remind_me_to_check_my_pipeline_every_morning')}</p>
              </div>

              {/* Pre-canned schedules pinned to the bottom: click to open the
                  create flow pre-filled. Only FEATURED presets surface here so
                  the empty state stays compact as the full catalog grows — the
                  rest live in the "Browse all templates" gallery. */}
              <div className="w-full shrink-0 pt-4 sm:pt-6">
                <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
                  <div className="text-left text-[12px] font-medium uppercase tracking-[.04em] text-muted">{i18nT('pages.schedulePage.start_from_a_pre_made_schedule')}</div>
                  <Btn onClick={() => setGalleryOpen(true)}>
                    <span className="flex items-center gap-1.5"><LayoutGrid size={14} aria-hidden="true" /> {i18nT('pages.schedulePage.browse_all_templates')}</span>
                  </Btn>
                </div>
                <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
                  {SCHEDULE_PRESETS.filter(p => p.featured).map(p => (
                    <Clickable
                      key={p.id}
                      onClick={() => openPreset(p)}
                      className="group flex flex-col items-start gap-2 text-left px-5 py-5 rounded-[20px] bg-card border border-border hover:border-accent/50 hover:bg-bg-hover transition-colors focus-ring cursor-pointer"
                    >
                      <span className="text-accent shrink-0">{p.icon}</span>
                      <span className="text-[15px] font-semibold text-text-strong leading-snug">{p.title}</span>
                      <span className="text-[13px] leading-[18px] text-muted">{p.description}</span>
                      <span className="flex items-center gap-2 mt-auto">
                        <span className="text-[12px] text-muted/80 font-medium">{formatCadence(p.prefill)}</span>
                        {p.writes && (
                          <span className="inline-flex items-center gap-1 text-[11px] font-medium text-warn-fg bg-warn-subtle rounded-full px-2 py-0.5" title={i18nT('pages.schedulePage.writes_badge_tooltip')}>
                            <GitPullRequestArrow size={11} aria-hidden="true" />
                            {i18nT('pages.schedulePage.writes_to_your_repos')}
                          </span>
                        )}
                      </span>
                    </Clickable>
                  ))}
                </div>
              </div>
            </div>
          ) : (<>
          {/* Create lives ABOVE the view switch so it survives Calendar and
              Executions — it used to sit in the card header, which every view
              shared. Filter, batch actions and New folder are list-only: there
              is nothing to filter or select in the other two. */}
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {jobsView === 'list' && (<>
              <div className="flex-1 min-w-[140px] sm:min-w-[200px]"><SearchInput placeholder={i18nT('pages.schedulePage.filter_jobs')} value={cronFilter} onChange={e => setCronFilter(e.target.value)} /></div>
              {selectedIds.size > 0 && (
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[13px] text-muted whitespace-nowrap">{selectedIds.size} {i18nT('pages.schedulePage.selected')}</span>
                  <Btn onClick={clearSelection}>{i18nT('pages.schedulePage.clear')}</Btn>
                  <CronJobMoveMenu
                    folders={cronFolders}
                    onMove={async (folderId) => {
                      const ids = Array.from(selectedIds)
                      const results = await Promise.allSettled(ids.map(id => api.updateCron(id, { folder_id: folderId })))
                      const failedIds = ids.filter((_, i) => results[i].status === 'rejected')
                      if (failedIds.length > 0) {
                        // Keep the failures selected so the user can retry the move.
                        setSelectedIds(new Set(failedIds))
                        setActionError({ id: 'batch-move', msg: i18nT('pages.schedulePage.cronFolders.batch_move_failed', { count: failedIds.length, total: ids.length }) })
                      } else {
                        setSelectedIds(new Set())
                      }
                      if (folderId) {
                        setCollapsedFolders(prev => {
                          if (!prev.has(folderId)) return prev
                          const next = new Set(prev)
                          next.delete(folderId)
                          saveCollapsedFolders(next)
                          return next
                        })
                      }
                      await load()
                    }}
                    onNewFolder={handleNewFolder}
                  />
                  <Btn danger onClick={openBatchConfirm} title={`Delete ${selectedIds.size} selected job(s)`}>
                    <span className="flex items-center gap-1.5"><Trash2 size={14} /> {i18nT('pages.schedulePage.delete')} {selectedIds.size} {i18nT('pages.schedulePage.selected')}</span>
                  </Btn>
                </div>
              )}
              <Btn onClick={() => handleNewFolder()}>
                <span className="flex items-center gap-1.5">
                  <FolderPlus size={14} aria-hidden="true" />
                  {i18nT('pages.schedulePage.cronFolders.new_folder')}
                </span>
              </Btn>
            </>)}
            <span className={jobsView === 'list' ? '' : 'ml-auto'}>
              <AddJobSplitButton onBlank={openBlankCreate} onBrowseTemplates={() => setGalleryOpen(true)} />
            </span>
          </div>
          {jobsView === 'calendar' ? (<>
              <div className="flex items-center gap-2 mb-3 text-[13px] text-muted">
                <Globe className="lucide-inline" />
                {/* Control is correctly associated via htmlFor+id (the select can't be nested); label-has-for's nesting requirement is a false positive here. */}
                <label htmlFor="schedule-render-tz" className="mr-1">{i18nT('pages.schedulePage.render_in')}</label>
                <TimezoneSelect id="schedule-render-tz" value={renderTz} onChange={setRenderTz} />
                <InfoTip text={i18nT('pages.schedulePage.changes_only_how_the_calendar_grid_is_displayed')} />
              </div>
              <WeekGrid jobs={jobs} selectedId={selected?.id} onSelect={openDetail} renderTz={renderTz} />
            </>) : jobsView === 'executions' ? (
              <ExecutionsView selectedJobId={selected?.id} />
            ) : (<>
            {/* The table sits in a Card so the data section reads as one framed
                block, matching the repo's page-layout pattern (HooksPage). No
                CardTitle: the page header already names this surface, and the
                view switcher next to it says which of the three views is on —
                a "Jobs" heading between them would restate both. */}
            <Card className="p-3 mb-0 overflow-x-auto">
            {actionError?.id === 'batch-move' && (
              <div className="px-3 py-1.5 mb-2 rounded-md bg-danger/5 border border-danger/20">
                <span className="text-danger text-[12px]">{actionError.msg}</span>
              </div>
            )}
            {/* `table-fixed`: the column widths below are a CONTRACT, not a
                hint. With auto layout a single long cell (an agent name, a cron
                expression) widens the whole table past its container and the
                Actions column walks off the right edge — which is what happened
                once cells stopped wrapping. Fixed layout makes horizontal
                overflow structurally impossible: over-long values truncate with
                a tooltip instead of pushing their neighbours.

                Every column carries a px width EXCEPT Message, which is the sole
                residual: it is the one column whose value has no natural length,
                so it should absorb every pixel the viewport has spare. Two rules
                keep that from turning into a collapse, both pinned by
                `SchedulePage.columnContract.test.ts`:

                - No PERCENTAGE widths. A percentage column grows with the table,
                  so it takes a share of exactly the width the residual column
                  needs. With the previous 15%/13%/12% the residual was
                  `0.6 × width − 540px`, i.e. ZERO at the table's own 900px
                  min-width — and a fixed layout does not shrink content to fit,
                  it overlaps the next cell. That is what put the Message chevron
                  and preview on top of the Status badge at phone widths, and on a
                  1280px desktop with the nav rail open (measured 0px there too).
                - `min-w` covers the nine px columns (996px, border-box, so the
                  `p-2`/`px-2` is inside each) PLUS a 180px floor for Message —
                  enough for the 14px chevron and a one-line preview. A narrower
                  container scrolls the table, which is honest; voiding a column
                  silently is not. Widening any px column means moving `min-w` by
                  the same amount: the two numbers are one statement, and editing
                  only the column takes the difference out of Message. */}
            <div className="relative">
            {/* The scroller is the shadcn Table's own wrapper (the table's
                parentElement — `relative w-full overflow-x-auto` in
                ui/table.tsx); `sticky right-0` on the Actions cells resolves
                against it, so the overflow measurement must read the same box.
                `className` stays the FIRST attribute: the columnContract test
                anchors on the literal `<Table className="table-fixed` opener. */}
            <Table className="table-fixed min-w-[1176px]" ref={attachJobsTable}>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="w-[36px] px-2 text-center">
                    <input
                      type="checkbox"
                      aria-label={i18nT('pages.schedulePage.select_all_jobs')}
                      title={i18nT('pages.schedulePage.select_deselect_all_jobs_matching_the_current_fi')}
                      className="accent-accent cursor-pointer align-middle"
                      checked={allVisibleSelected}
                      ref={el => { if (el) el.indeterminate = !allVisibleSelected && someVisibleSelected }}
                      onChange={toggleAllVisible}
                    />
                  </TableHead>
                  <TableHead className="w-[68px]">{i18nT('pages.schedulePage.id')}</TableHead>
                  <SortableTableHead label={i18nT('pages.schedulePage.name')} sortKey="name" sort={schedSort} onToggle={toggleSchedSort} className="w-[160px]" />
                  <TableHead className="w-[116px]">{i18nT('pages.schedulePage.type')}</TableHead>
                  {/* 180px, not the original 124: the value here is a clock time
                      plus a qualifier (`12:00 AM · Mon,Wed`), and 124px fitted
                      the time alone -- so every midnight job rendered the same
                      truncated string and the column stopped distinguishing
                      rows. Widening is paid for in the table's min-width below,
                      NOT out of Message, which keeps its floor. */}
                  <SortableTableHead label={i18nT('pages.schedulePage.schedule')} sortKey="schedule" sort={schedSort} onToggle={toggleSchedSort} className="w-[180px]" />
                  <TableHead>{i18nT('pages.schedulePage.message')}</TableHead>
                  <SortableTableHead label={i18nT('pages.schedulePage.status')} sortKey="status" sort={schedSort} onToggle={toggleSchedSort} className="w-[86px]" />
                  <SortableTableHead label={i18nT('pages.schedulePage.last_run')} sortKey="lastRun" sort={schedSort} onToggle={toggleSchedSort} className="w-[82px]" />
                  <SortableTableHead label={i18nT('pages.schedulePage.next_run')} sortKey="nextRun" sort={schedSort} onToggle={toggleSchedSort} className="w-[92px]" />
                  {/* 176px, not 164: under `table-fixed` a column width is a
                      CONTRACT, so a cell whose controls need more than it spills
                      OUT of the table instead of widening it. The three inline
                      controls (Run/Cancel, Delete, the ⋯ menu) measure 176px, and
                      the 12px shortfall was being hidden by the shadcn wrapper's
                      overflow-x-auto — i.e. a horizontal scrollbar, which this
                      table must never need.

                      `sticky right-0`: Actions is the last of ten columns, so in
                      a container narrower than the table's min-width it starts
                      past the scroll edge and every Run/Delete costs a horizontal
                      scroll. Pinning it to the scrollport's right edge keeps row
                      actions reachable while the other columns scroll under it —
                      which is why the cell needs an OPAQUE `bg-card` (the Card's
                      own surface): the default cell background is transparent and
                      the scrolling columns would show through. Sticky changes
                      paint position, not column width, so the `w-[176px]`
                      contract above still holds. The seam is TWO parts, both
                      gated on the measured overflow flag so a full-width table
                      renders neither: a 1px child div in this cell (legible over
                      whitespace, where a fade into the same surface colour
                      vanishes — a child div and not `border-l`, because under
                      Preflight's `border-collapse: collapse` a cell border
                      belongs to the collapsed table grid and stays at the
                      cell's layout slot instead of travelling with the sticky
                      cell) and the gradient painted after the table (says
                      "content continues", which a 1px rule alone does not). */}
                  <TableHead className="sticky right-0 w-[176px] bg-card">
                    {jobsTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                    {i18nT('pages.schedulePage.actions')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>{jobs.length === 0
              ? <TableRow className="hover:bg-transparent"><TableCell colSpan={SCHEDULE_COLUMNS}><EmptyState icon={<ClipboardList className="lucide-inline" />} title={i18nT('pages.schedulePage.no_cron_jobs')} /></TableCell></TableRow>
              : sortedScheduleJobs.length === 0
              ? <TableRow className="hover:bg-transparent"><TableCell colSpan={SCHEDULE_COLUMNS}><FilteredEmpty query={cronFilter} onClear={() => setCronFilter('')} noun={i18nT('pages.schedulePage.jobs_noun')} /></TableCell></TableRow>
              : (() => {
                const groups = groupJobsByFolder(sortedScheduleJobs, cronFolders, { omitEmpty: !!cronFilter })
                const hasFolders = cronFolders.length > 0
                return groups.map(group => {
                  const folderId = group.folder?.id
                  // Fix #3: bypass persisted collapse state while filter is active
                  const isCollapsed = cronFilter ? false : (folderId ? collapsedFolders.has(folderId) : false)
                  return (
                    <Fragment key={`group-${folderId || 'ungrouped'}`}>{group.folder && (
                      <CronFolderHeader
                        key={`fh-${folderId}`}
                        folder={group.folder}
                        jobCount={group.jobs.length}
                        collapsed={isCollapsed}
                        onToggleCollapse={() => folderId && toggleFolderCollapse(folderId)}
                        onRename={async (name) => { if (folderId) { try { await api.updateCronFolder(folderId, { name }); await refreshFolders() } catch (e) { setActionError({ id: `folder-${folderId}`, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.failed') }) } } }}
                        onDelete={() => folderId && handleDeleteFolder(folderId)}
                        colSpan={SCHEDULE_COLUMNS}
                      />
                    )}
                    {group.folder && actionError?.id === `folder-${folderId}` && (
                      <TableRow key={`fe-${folderId}`} className="border-danger/20 hover:bg-transparent">
                        <TableCell colSpan={SCHEDULE_COLUMNS} className="px-4 py-1.5">
                          <span className="text-danger text-[12px]">{actionError.msg}</span>
                        </TableCell>
                      </TableRow>
                    )}
                    {!group.folder && hasFolders && group.jobs.length > 0 && (
                      <TableRow key="ungrouped-header" className="bg-bg-elevated/30 hover:bg-transparent">
                        <TableCell colSpan={SCHEDULE_COLUMNS} className="px-2.5 py-1.5 text-[12px] text-muted font-medium">
                          {i18nT('pages.schedulePage.cronFolders.ungrouped')}
                        </TableCell>
                      </TableRow>
                    )}
                    {!isCollapsed && group.jobs.map(j => (
              <TableRow key={j.id} className={`group/jobrow cursor-pointer ${selected?.id === j.id ? 'bg-accent-subtle' : ''} ${selectedIds.has(j.id) ? 'bg-accent-subtle/60' : ''}`} onClick={() => openDetail(j)}>
                <TableCell className="px-2 text-center" onClick={e => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    aria-label={i18nT('pages.schedulePage.select', { name: j.name })}
                    className="accent-accent cursor-pointer align-middle"
                    checked={selectedIds.has(j.id)}
                    onChange={() => toggleOne(j.id)}
                  />
                </TableCell>
                <TableCell className="truncate"><code>{j.id}</code></TableCell>
                {/* Name on line 1, its owning session on line 2 — same pairing
                    as the Type and Schedule columns. The empty state renders
                    EXPLICIT copy, italic prose against the owned state's mono,
                    because "no owning session" is the fact that explains why a
                    job is invisible to cron_list in chat — a blank line would
                    hide exactly the state this line exists to show. */}
                <TableCell className="truncate text-text-strong" title={`${j.name} · ${j.session_key ? i18nT('pages.schedulePage.owning_session_tooltip', { key: j.session_key }) : i18nT('pages.schedulePage.no_owning_session')}`}>
                  <span className="flex items-center gap-1.5 min-w-0">
                    <span className="block truncate min-w-0">{j.name}</span>
                    {/* A pending secret request otherwise lives only inside the
                        detail dialog (the chat card is best-effort), so the row
                        carries the signal that something awaits approval. */}
                    {j.secret_env_pending && Object.keys(j.secret_env_pending).length > 0 && (
                      <Badge variant="warn" title={i18nT('pages.schedulePage.secrets_pending_badge')}>
                        <KeyRound size={11} className="lucide-inline" aria-hidden="true" />
                        <span className="sr-only">{i18nT('pages.schedulePage.secrets_pending_badge')}</span>
                      </Badge>
                    )}
                  </span>
                  {j.session_key
                    ? <span className="block truncate text-[11px] font-mono font-normal text-muted">{j.session_key}</span>
                    : <span className="block truncate text-[11px] italic font-normal text-muted">{i18nT('pages.schedulePage.no_owning_session')}</span>}
                </TableCell>
                {/* Kind on line 1, its owner on line 2 — mirrors the
                    schedule/timezone pair in the next column. The agent's model
                    is tooltip-only: at this width it truncated to noise, and the
                    detail dialog shows it in full. */}
                <TableCell className="truncate" title={j.script ? j.script : j.command ? j.command : `${agentOrDefaultLabel(j.agent, defaultAgent)}${j.model ? ` · ${j.model}` : ''}`}>
                  {j.script ? <span className="font-medium text-[var(--accent)]">{i18nT('pages.schedulePage.script_python')}</span>
                    : j.command ? <span className="font-medium text-[var(--warn)]">{i18nT('pages.schedulePage.command_shell')}</span>
                    : <>
                        <span className="text-muted">{i18nT('pages.schedulePage.agent')}</span>
                        <span className="block truncate text-[11px] text-muted">{agentOrDefaultLabel(j.agent, defaultAgent)}</span>
                      </>}
                </TableCell>
                {/* The compact label in the cell, the verbose one in the
                    tooltip. `schedule` is cron_descriptor prose -- 53 chars for
                    `0 0 30 2 *` -- so at any width this column can afford, it
                    renders as "At 12:00 AM ...", which is the SAME string for
                    every midnight job: the part identifying the schedule is
                    exactly the part that gets clipped.

                    Derived HERE from the already-shipped `cron_expr` rather than
                    minted server-side, so the day and month names translate with
                    the dashboard through `fmtWeekday` / `Intl` instead of pinning
                    English into the API. A non-cron schedule (interval, one-shot)
                    has no `cron_expr` and already reads compactly, so it keeps
                    the backend string.

                    No `<code>`: the short form is prose, and monospace is the
                    WIDEST rendering available for the least code-like value on
                    the page -- it was spending the column's pixels to fit fewer
                    characters. `fmtCron`'s raw-expression fallback stays legible
                    without it. */}
                <TableCell className="truncate" title={j.schedule}>{scheduleLabel(j)}{j.timezone && <span className="block truncate text-[11px] text-muted">{j.timezone.replace(/_/g, ' ')}</span>}</TableCell>
                <TableCell className="align-top"><CollapsibleMessage message={j.script ? j.script : j.command ? j.command : j.safeMessage} /></TableCell>
                <TableCell title={j.last_error || j.last_result || ''}>{j.is_running ? <Badge variant="ok"><span className="inline-block w-1.5 h-1.5 rounded-full bg-ok animate-pulse mr-1 align-middle" />{i18nT('pages.schedulePage.running')}</Badge> : j.enabled ? (j.last_status === 'ok' ? <Badge variant="ok">{i18nT('pages.schedulePage.ok')}</Badge> : j.last_status === 'error' ? <Badge variant="err">{i18nT('pages.schedulePage.error')}</Badge> : <Badge variant="ok">{i18nT('pages.schedulePage.ready')}</Badge>) : <Badge variant="warn">{i18nT('pages.schedulePage.paused')}</Badge>}</TableCell>
                <TableCell className="text-muted">{fmtAgo(j.last_run_ts)}</TableCell>
                <TableCell className="text-muted" title={j.next_run_ts ? fmtDateTimeNumeric(j.next_run_ts) : ''}>{fmtIn(j.next_run_ts)}</TableCell>
                {/* Two controls plus the overflow menu. Anything wider than this
                    is what pushed the column off screen; see CronRowActions.
                    Pinned like the header cell above, on an OPAQUE `bg-card`.
                    The row's hover/selected tints live on the <tr>, which the
                    opaque base would hide, so the overlay div re-applies the
                    SAME tokens above the base and below the controls (`-z-10`
                    inside the stacking context every sticky cell creates):
                    `--accent-subtle` is translucent, so composited over
                    `bg-card` it matches the rest of the row exactly. */}
                <TableCell className="sticky right-0 whitespace-nowrap bg-card" onClick={e => e.stopPropagation()}>
                  <div aria-hidden className={`absolute inset-0 -z-10 transition-colors group-hover/jobrow:bg-bg-hover ${selected?.id === j.id ? 'bg-accent-subtle' : ''} ${selectedIds.has(j.id) ? 'bg-accent-subtle/60' : ''}`} />
                  {jobsTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                  <div className="flex items-center gap-1.5">
                    {j.is_running
                      ? <span title={i18nT('pages.schedulePage.cancel_running_execution')}><Btn danger onClick={() => cancelRun(j.id)} disabled={cancelling.has(j.id)}>{cancelling.has(j.id) ? '...' : i18nT('pages.schedulePage.cancel')}</Btn></span>
                      : <span title={j.enabled ? i18nT('pages.schedulePage.run_now_2') : i18nT('pages.schedulePage.resume_to_run')}><Btn onClick={() => runNow(j.id)} disabled={!j.enabled || running.has(j.id)}>{running.has(j.id) ? '...' : i18nT('pages.schedulePage.run')}</Btn></span>}
                    {/* The armed state must explain itself IN THE LABEL: the
                        `title` tooltip below is hover-only, so on touch it does
                        not exist, and a bare "Confirm" gives a phone user no
                        statement of what the second tap will destroy (#4120).
                        The tooltip stays as a redundant pointer affordance.
                        The visible text is also the accessible name — no
                        aria-label, which would override the label a sighted
                        user reads and break WCAG 2.5.3 (Label in Name); the
                        row context names the job. Same convention as
                        ChatInput's Continue/Send buttons. */}
                    <Btn
                      danger
                      disabled={isDeleting(j.id)}
                      title={confirmDeleteId === j.id ? i18nT('pages.schedulePage.click_again_to_confirm') : i18nT('pages.schedulePage.delete_job')}
                      onClick={() => { if (confirmDeleteId === j.id) void confirmDelete(j.id); else armDelete(j.id) }}
                    >{isDeleting(j.id) ? '...' : confirmDeleteId === j.id ? i18nT('pages.schedulePage.confirm_delete_job') : i18nT('pages.schedulePage.delete')}</Btn>
                    <CronRowActions
                      job={j}
                      folders={cronFolders}
                      running={running.has(j.id)}
                      cancelling={cancelling.has(j.id)}
                      onRun={() => runNow(j.id)}
                      onCancelRun={() => cancelRun(j.id)}
                      onOpenInChat={() => openInChat(j.id)}
                      onToggleEnabled={async () => { try { await api.toggleCron(j.id, !j.enabled); load() } catch (e: unknown) { setActionError({ id: j.id, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.failed') }) } }}
                      onToggleStrict={async () => { try { await api.updateCron(j.id, { strict_schedule: !j.strict_schedule }); load() } catch (e: unknown) { setActionError({ id: j.id, msg: e instanceof Error ? e.message : i18nT('pages.schedulePage.failed') }) } }}
                      onMove={fid => handleMoveJob(j.id, fid)}
                      onNewFolder={handleNewFolder}
                    />
                  </div>
                  {actionError?.id === j.id && <div className="mt-1 text-danger text-[12px]">{actionError.msg}</div>}
                </TableCell>
              </TableRow>
                    ))}</Fragment>
                  )
                })
              })()}</TableBody></Table>
            {/* Seam cue for the pinned Actions column, same measured treatment
                as the strips that already ship it (SessionRefStrip, FollowUpBar,
                SidePanelLayout): a gradient says columns continue beneath the
                pinned cell, because the opaque base otherwise hard-clips its
                neighbour mid-word with no sign the table scrolls. Anchored to
                the non-scrolling wrapper (a child of the scroller would travel
                with the content) at the pinned column's left edge, and painted
                ONLY while the scroller actually hides columns — a full-width
                table shows nothing. `from-card` blends the clipped content into
                the pinned cell's own surface. */}
            {jobsTableEdges.right && (
              <div aria-hidden="true" data-testid="jobs-table-cue-right" className="pointer-events-none absolute right-[176px] top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />
            )}
            </div>
            </Card>
            </>)}
          </>)}
        </div>
      </div>

      <Dialog open={detailDialogOpen} onOpenChange={next => { if (!next) closeDetail() }}>
        {detailDialogOpen && (
          <JobDetailDialog
            key={creating ? (prefill ? `preset#${prefillNonce}` : 'new') : selected?.id}
            job={creating ? undefined : selected || undefined}
            prefill={creating ? prefill || undefined : undefined}
            prefillWrites={creating && !!prefill && prefillWrites}
            agents={agents}
            defaultAgent={defaultAgent}
            rosterFailure={rosterFailure}
            onClose={closeDetail}
            onSaved={() => { load(); closeDetail() }}
          />
        )}
      </Dialog>

      <Dialog
        open={!!folderModal}
        onOpenChange={next => { if (!next) setFolderModal(prev => { prev?.resolve?.(undefined); return null }) }}
      >
        <DialogContent maxWidth={360}>
          <DialogHeader>
            <DialogTitle>{i18nT('pages.schedulePage.cronFolders.new_folder')}</DialogTitle>
          </DialogHeader>
          <DialogBody>
            <Input
              autoFocus
              aria-label={i18nT('pages.schedulePage.cronFolders.new_folder_name')}
              value={folderModalName}
              onChange={e => setFolderModalName(e.target.value)}
              {...folderNameIme.bindComposition()}
              onKeyDown={e => {
                if (e.key !== 'Enter') return
                // Rule 1: single-line input; emptiness stays outside the guard.
                if (folderNameIme.isComposing(e)) return
                if (folderModalName.trim()) handleFolderModalSubmit()
              }}
              placeholder={i18nT('pages.schedulePage.cronFolders.new_folder_name')}
              className="w-full"
            />
            {folderModalError && <p className="text-danger text-[12px] mt-3">{folderModalError}</p>}
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => { setFolderModal(prev => { prev?.resolve?.(undefined); return null }) }}>{i18nT('pages.schedulePage.cancel')}</Btn>
            <SendBtn onClick={handleFolderModalSubmit} disabled={!folderModalName.trim()}>
              {i18nT('pages.schedulePage.cronFolders.create')}
            </SendBtn>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ScheduleTemplateGallery
        open={galleryOpen}
        onClose={() => setGalleryOpen(false)}
        onPick={p => { setGalleryOpen(false); openPreset(p) }}
      />

      <Dialog open={batchConfirm} onOpenChange={next => { if (!next && !batchDeleting) setBatchConfirm(false) }}>
        <DialogContent maxWidth={460}>
          <DialogHeader>
            <Trash2 size={16} className="text-danger shrink-0" aria-hidden="true" />
            <DialogTitle>
              {i18nT('pages.schedulePage.delete')} {i18nT('pages.schedulePage.scheduled_job', { count: selectedIds.size })}?
            </DialogTitle>
          </DialogHeader>
          <DialogBody>
            <DialogDescription className="mb-3">{i18nT('pages.schedulePage.this_permanently_removes_the_selected_job', { count: selectedIds.size })} {i18nT('pages.schedulePage.and_their_run_history_this_action_cannot_be_undo')}</DialogDescription>
            <div className="max-h-[168px] overflow-y-auto rounded-md border border-border bg-bg divide-y divide-border/60 mb-4">
              {selectedJobs.map(jb => (
                <div key={jb.id} className="flex items-center gap-2 px-3 py-1.5 text-[13px]">
                  <code className="text-muted shrink-0">{jb.id}</code>
                  <span className="truncate text-text">{jb.name}</span>
                </div>
              ))}
            </div>
            <label htmlFor="batch-delete-confirm" className="block text-[13px] text-muted mb-1.5">
              {/* `delete` is a LITERAL safety token compared verbatim against the
                  input (see `confirmArmed`), not display copy. Translating it
                  makes the confirm button impossible to arm in that language —
                  a zh-CN user typed the displayed 删除 and bulk delete stayed
                  disabled. Keep it untranslated.

                  The key is `type_verb_to_confirm`, NOT the `type` used by the
                  table header above: English "Type" is both a noun (the column)
                  and an imperative verb (this instruction), and no single
                  translation serves both. Sharing one key made es/pt render the
                  NOUN here ("Tipo delete para confirmar"), turning the
                  instruction into a fragment. A key whose name states the part
                  of speech is what keeps a translator from having to guess. */}
              {i18nT('pages.schedulePage.type_verb_to_confirm')} <code className="text-text font-semibold">{BULK_DELETE_TOKEN}</code> {i18nT('pages.schedulePage.to_confirm')}
            </label>
            <input
              id="batch-delete-confirm"
              autoFocus
              value={confirmText}
              onChange={e => setConfirmText(e.target.value)}
              {...batchConfirmIme.bindEnter({
                onEnter: () => { if (confirmArmed && !batchDeleting) runBatchDelete() },
              })}
              placeholder={BULK_DELETE_TOKEN}
              className="w-full px-3 py-2 rounded-md bg-bg border border-border text-sm text-text outline-none focus-visible:border-accent"
            />
            {batchError && <p className="text-danger text-[12px] mt-2">{batchError}</p>}
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => setBatchConfirm(false)} disabled={batchDeleting}>{i18nT('pages.schedulePage.cancel')}</Btn>
            <Btn danger disabled={batchDeleting || !confirmArmed} onClick={runBatchDelete}>
              {batchDeleting ? i18nT('pages.schedulePage.deleting') : i18nT('pages.schedulePage.delete_2', { n: selectedIds.size })}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

/**
 * Collapsed-by-default source view for a script cron's callable.
 *
 * Collapsed so the detail dialog does not grow taller for every job, and the
 * source is fetched lazily on first expand (React Query holds `enabled: false`
 * until then), so opening the dialog costs no extra request. The backend
 * derives the file path from the job's own stored `script` field — the job id
 * is the only input this component sends.
 */
function ScriptSourcePanel({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false)
  const { data, isPending, isError, error } = useQuery({
    queryKey: ['cronScript', jobId],
    queryFn: async () => (await api.cronScript(jobId)) as CronScriptSource,
    enabled: open,
    // The file on disk can change between expands (scripts are agent-editable),
    // and the app-wide default is staleTime: Infinity — opt this query out so
    // re-expanding always refetches the current source.
    staleTime: 0,
  })
  const download = useCallback(() => {
    if (!data) return
    const blob = new Blob([data.source], { type: 'text/x-python' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = data.file || 'script.py'
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }, [data])
  return (
    <div className="flex flex-col gap-1.5">
      <Clickable
        className="flex items-center gap-1 w-fit text-[12px] text-muted font-medium hover:text-text cursor-pointer"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
      >
        <ChevronRight size={14} className={`lucide-inline transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
        {i18nT('pages.schedulePage.script_source')}
      </Clickable>
      {open && isPending && <Skeleton className="h-16 rounded-xl" />}
      {open && isError && (
        <div className="text-danger text-[13px]">
          {error instanceof Error && error.message ? error.message : i18nT('pages.schedulePage.script_source_failed')}
        </div>
      )}
      {open && data && (
        <>
          <CodeBlock
            code={data.source}
            lang="python"
            complete
            headerActions={
              <Btn
                className="p-1 border-0 rounded text-muted hover:text-text hover:bg-bg-hover"
                onClick={download}
                title={i18nT('pages.schedulePage.script_source_download')}
                aria-label={i18nT('pages.schedulePage.script_source_download')}
              >
                <Download size={13} className="lucide-inline" aria-hidden="true" />
              </Btn>
            }
          />
          {data.truncated && (
            <div className="text-[12px] text-muted">{i18nT('pages.schedulePage.script_source_truncated')}</div>
          )}
        </>
      )}
    </div>
  )
}

/** Shape of GET /api/crons/{id}/script. */
type CronScriptSource = {
  source: string
  file: string
  function: string
  truncated: boolean
  /**
   * Server verdict that the displayed source IS the raw body (not truncated,
   * decoded losslessly, nothing masked by redaction). Only a reviewable body
   * is approvable — the server re-derives this on approve, so this flag is a
   * UX gate, not the enforcement.
   */
  reviewable: boolean
  /** Digest of the raw source bytes; an approval must echo it back. */
  sha256: string
}

/**
 * Vault-secret grants for a script/command job — the operator half of the
 * agent-first flow. Renders the agent's pending request as an approve/deny
 * banner (approval re-verifies the request's code pin server-side), the
 * active grant, and a small direct-grant editor. Env-var names and vault
 * secret NAMES only; values never reach this page.
 */
export function JobSecretsPanel({ job, onSaved }: { job: CronJob; onSaved: () => void }) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pending = job.secret_env_pending ?? null
  const active = job.secret_env ?? {}
  // The script the approval would bless, loaded INSIDE the banner and keyed
  // to the pending request's revision: an agent can rewrite the script and
  // re-issue the request while this page is open, and the job refresh that
  // swaps the banner to the new request must swap the source with it — a
  // source view cached by job id alone would keep showing the old code under
  // the new request's approve button. Approval stays disabled until this
  // exact revision's source has rendered, and the approve call echoes its
  // digest so the server refuses to promote code the operator did not see.
  const source = useQuery({
    queryKey: ['cronScript', job.id, 'pending', job.secret_env_pending_ts ?? 0],
    queryFn: async () => (await api.cronScript(job.id)) as CronScriptSource,
    enabled: pending !== null,
    staleTime: 0,
  })
  const reviewed = source.data && source.data.reviewable ? source.data : null
  const grant = useMutation({
    mutationFn: (body: Parameters<typeof api.cronSecretsGrant>[1]) =>
      api.cronSecretsGrant(job.id, body),
    onMutate: () => setError(null),
    onSuccess: () => onSaved(),
    onError: (e: unknown) =>
      setError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed')),
  })
  const busy = grant.isPending
  const act = (body: Parameters<typeof api.cronSecretsGrant>[1]) => grant.mutate(body)
  // Revoking is one click with an expensive recovery (the agent must
  // re-request, the operator must re-review and re-approve), so it takes the
  // same arm-then-confirm gesture the page's Delete already uses.
  const revoke = useArmedDelete(async () => {
    setError(null)
    try {
      await api.cronSecretsGrant(job.id, { secret_env: {} })
      onSaved()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed'))
    }
  })
  const revokeArmed = revoke.armedId === job.id
  // Both lists read "ENV ← vault name"; the arrow alone carries the direction
  // (and is hidden from assistive tech), so the caption states it in words.
  const directionCaption = (
    <div className="text-[11px] text-muted">{i18nT('pages.schedulePage.secrets_direction_caption')}</div>
  )
  return (
    <div className="flex flex-col gap-1.5">
      {pending && (
        <div className="flex flex-col gap-2 px-3 py-2.5 rounded-lg bg-warn-subtle text-warn-fg" role="note">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold">
            <KeyRound size={14} className="lucide-inline shrink-0" aria-hidden="true" />
            {i18nT('pages.schedulePage.secrets_pending_title')}
          </div>
          {directionCaption}
          <ul className="flex flex-col gap-0.5 text-[12.5px] font-mono">
            {Object.entries(pending).map(([env, name]) => (
              <li key={env} className="min-w-0 break-all">{env} ← {name}</li>
            ))}
          </ul>
          <div className="text-[12px] opacity-90">{i18nT('pages.schedulePage.secrets_pending_help')}</div>
          <div className="text-[12px] font-medium">{i18nT('pages.schedulePage.secrets_pending_source')}</div>
          {source.isPending && (
            <>
              <Skeleton className="h-16 rounded-xl" />
              <div className="text-[12px] opacity-90">{i18nT('pages.schedulePage.secrets_pending_source_loading')}</div>
            </>
          )}
          {source.isError && (
            <ErrorNotice message={i18nT('pages.schedulePage.secrets_pending_source_failed')} askAgent />
          )}
          {source.data && source.data.truncated && (
            <ErrorNotice message={i18nT('pages.schedulePage.secrets_pending_source_truncated')} askAgent />
          )}
          {source.data && !source.data.truncated && !source.data.reviewable && (
            <ErrorNotice message={i18nT('pages.schedulePage.secrets_pending_source_unreviewable')} askAgent />
          )}
          {source.data && <CodeBlock code={source.data.source} lang="python" complete />}
          <div className="flex gap-2">
            <SendBtn
              disabled={busy || !reviewed}
              onClick={() =>
                reviewed &&
                act({
                  approve_pending: true,
                  // Restate what THIS banner displayed: the backend refuses
                  // (409 stale_request) if the pending request was replaced
                  // after render, so an unseen request can never be approved.
                  expected_secret_env: pending,
                  expected_ts: job.secret_env_pending_ts ?? undefined,
                  // ...and the digest of the source rendered above (409
                  // stale_source if the file no longer matches it).
                  expected_source_sha256: reviewed.sha256,
                })
              }
            >
              {i18nT('pages.schedulePage.secrets_approve')}
            </SendBtn>
            <Btn
              danger
              disabled={busy}
              onClick={() =>
                act({
                  deny_pending: true,
                  expected_secret_env: pending,
                  // The timestamp distinguishes a REISSUED request with an
                  // identical mapping from the one this banner displayed —
                  // a stale denial must not delete the reissue.
                  expected_ts: job.secret_env_pending_ts ?? undefined,
                })
              }
            >
              {i18nT('pages.schedulePage.secrets_deny')}
            </Btn>
          </div>
        </div>
      )}
      <Clickable
        className="flex items-center gap-1 w-fit text-[12px] text-muted font-medium hover:text-text cursor-pointer"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
      >
        <ChevronRight size={14} className={`lucide-inline transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
        {i18nT('pages.schedulePage.secrets_section')}
        {Object.keys(active).length > 0 && <Badge variant="ok">{Object.keys(active).length}</Badge>}
      </Clickable>
      {open && (
        <div className="flex flex-col gap-2">
          {Object.keys(active).length === 0 && (
            <div className="text-[12.5px] text-muted">
              {i18nT('pages.schedulePage.secrets_none')}{' '}
              {/* Grants are minted only by an agent-side request, so the empty
                  state has to say where the first grant comes from. */}
              {i18nT('pages.schedulePage.secrets_none_hint')}
            </div>
          )}
          {Object.keys(active).length > 0 && directionCaption}
          {Object.entries(active).map(([env, name]) => (
            <div key={env} className="flex items-start gap-2 text-[12.5px] min-w-0">
              <code className="font-mono text-text min-w-0 break-all">{env}</code>
              <span className="text-muted shrink-0" aria-hidden="true">←</span>
              <code className="font-mono text-muted min-w-0 break-all">{name}</code>
            </div>
          ))}
          <div className="text-[12px] text-muted">{i18nT('pages.schedulePage.secrets_active_help')}</div>
          {Object.keys(active).length > 0 && (
            <Btn
              danger
              disabled={busy || revoke.isDeleting(job.id)}
              className="w-fit"
              title={revokeArmed ? i18nT('pages.schedulePage.click_again_to_confirm') : undefined}
              onClick={() => { if (revokeArmed) void revoke.confirm(job.id); else revoke.arm(job.id) }}
            >
              {revoke.isDeleting(job.id)
                ? '...'
                : revokeArmed
                  ? i18nT('pages.schedulePage.secrets_revoke_all_confirm')
                  : i18nT('pages.schedulePage.secrets_revoke_all')}
            </Btn>
          )}
        </div>
      )}
      <ErrorNotice message={error} askAgent />
    </div>
  )
}

/**
 * Job detail / create view, rendered as a shadcn (Radix) dialog.
 *
 * Was a resizable side panel pinned to the right of the job list. The dialog
 * form factor follows the same migration the Crews page already made
 * (`KiroCrewAgentsPage`): one modal surface, focus trap, Escape-to-dismiss and
 * overlay behaviour owned by Radix instead of a hand-rolled backdrop.
 *
 * Two capabilities of the old panel are deliberately gone with it: the drag
 * splitter (a modal has a fixed width) and viewing the form side by side with
 * the list. The job selection they were paired with is NOT gone — the caller
 * keeps `selected` alive across dismissal so the calendar highlight and the
 * Executions filter survive.
 */
function JobDetailDialog({ job, prefill, prefillWrites, agents, defaultAgent, rosterFailure, onClose, onSaved }: {
  job?: CronJob; prefill?: CronPrefill; prefillWrites?: boolean; agents: KiroCrewAgent[]; defaultAgent: string; rosterFailure?: { reloading: boolean; onReload: () => void }; onClose: () => void; onSaved: () => void
}) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [panelError, setPanelError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<'details' | 'logs'>('details')
  useEffect(() => { setDetailTab('details') }, [job?.id])
  const submitRef = useRef<(() => void) | null>(null)

  return (
    <DialogContent maxWidth={720} className="max-h-[86vh]">
      <DialogHeader>
        <DialogTitle className="truncate">{job ? job.name : (prefill?.name || i18nT('pages.schedulePage.new_job'))}</DialogTitle>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-4">
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
              <Btn onClick={async () => { try { await api.toggleCron(job.id, !job.enabled); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed')) } }}>{job.enabled ? i18nT('pages.schedulePage.pause') : i18nT('pages.schedulePage.resume')}</Btn>
              {job.is_running
                ? <Btn danger onClick={async () => { try { await api.cancelCron(job.id); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed')) } }}>{i18nT('pages.schedulePage.cancel_run')}</Btn>
                : <SendBtn onClick={async () => { try { await api.runCron(job.id); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed')) } }}>{i18nT('pages.schedulePage.run_now')}</SendBtn>}
            </div>
          </div>
        )}
        {!job && (
          <div className="flex items-center justify-between">
            <Badge variant="ok">{i18nT('pages.schedulePage.new')}</Badge>
          </div>
        )}
        {detailTab === 'logs' && job ? (
          <JobLogsView jobId={job.id} isRunning={job.is_running} runningSince={job.running_since} cancelError={panelError} onCancel={async () => { setPanelError(null); try { await api.cancelCron(job.id); onSaved() } catch (e: unknown) { setPanelError(e instanceof Error ? e.message : i18nT('pages.schedulePage.failed')) } }} />
        ) : (
          <>
            {prefillWrites && (
              <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-warn-subtle text-[12.5px] text-warn-fg" role="note">
                <GitPullRequestArrow size={14} className="shrink-0 mt-0.5" aria-hidden="true" />
                <span>{i18nT('pages.schedulePage.writes_notice')}</span>
              </div>
            )}
            <JobForm job={job} prefill={prefill} agents={agents} defaultAgent={defaultAgent} rosterFailure={rosterFailure} onSaved={onSaved} layout="vertical" externalSubmit submitRef={submitRef} onSavingChange={setSaving} />
            {panelError && <div className="text-danger text-[13px]">{panelError}</div>}
            {job?.script && <ScriptSourcePanel jobId={job.id} />}
            {job && job.script && <JobSecretsPanel job={job} onSaved={onSaved} />}
            {job?.script && (job.last_result || job.last_error) && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[12px] text-muted font-medium">{job.last_error ? i18nT('pages.schedulePage.last_error') : i18nT('pages.schedulePage.last_output')}</div>
                <pre className={`text-[12px] font-mono whitespace-pre-wrap break-words rounded border px-2.5 py-2 max-h-[200px] overflow-y-auto ${job.last_error ? 'bg-danger/5 border-danger/20 text-danger' : 'bg-bg-elevated border-border text-text'}`}>{job.last_error || job.last_result}</pre>
              </div>
            )}
            {job?.last_run_ts && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[12px] text-muted font-medium">{i18nT('pages.schedulePage.last_run')}</div>
                <span className="text-sm text-text">{fmtDateTimeNumeric(job.last_run_ts)}</span>
              </div>
            )}
            {/* The row's owner line truncates; here the full key is readable.
                The ownerless copy stays italic-vs-mono distinguishable, same
                treatment as the table row. The helper sentence renders ONLY in
                the ownerless state: it explains that state's consequence (the
                job is invisible to cron_list in chat) and remedy, and under a
                live key the same sentence would read as a warning about the
                job in front of the reader. aria-describedby ties it to the
                value so a screen reader hears it as a hint, not a second
                label. */}
            {job && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[12px] text-muted font-medium">{i18nT('pages.schedulePage.owning_session')}</div>
                {job.session_key
                  ? <code className="text-[12px] font-mono break-all text-text">{job.session_key}</code>
                  : <>
                      <span className="text-sm italic text-muted" aria-describedby="owning-session-help">{i18nT('pages.schedulePage.no_owning_session')}</span>
                      <span id="owning-session-help" className="text-[12px] text-muted">{i18nT('pages.schedulePage.owning_session_help')}</span>
                    </>}
              </div>
            )}
          </>
        )}
      </DialogBody>
      <DialogFooter className="justify-between">
        {job ? (
          <Btn danger onClick={() => setConfirmDelete(true)}>
            <span className="flex items-center gap-1.5">
              <Trash2 size={14} aria-hidden="true" />
              {i18nT('pages.schedulePage.delete')}
            </span>
          </Btn>
        ) : <div />}
        <div className="flex items-center gap-2">
          <Btn onClick={onClose}>{i18nT('pages.schedulePage.cancel')}</Btn>
          <SendBtn onClick={() => submitRef.current?.()} disabled={saving}>
            <SaveCreateLabel isEdit={!!job} saving={saving} />
          </SendBtn>
        </div>
      </DialogFooter>
      {/* Nested confirm. `z-[110]` clears the parent dialog's z-[101] — the same
          stacking the Crews page uses for its create-dialog-over-detail case. */}
      {job && (
        <Dialog open={confirmDelete} onOpenChange={next => { if (!next && !deleting) setConfirmDelete(false) }}>
          <DialogContent maxWidth={360} className="z-[110]">
            <DialogHeader>
              <DialogTitle>{i18nT('pages.schedulePage.delete_named_job', { name: job.name })}</DialogTitle>
            </DialogHeader>
            <DialogBody>
              <DialogDescription>{i18nT('pages.schedulePage.this_will_permanently_remove_the_scheduled_job_t')}</DialogDescription>
              {deleteError && <p className="text-danger text-[12px] mt-2">{deleteError}</p>}
            </DialogBody>
            <DialogFooter>
              <Btn onClick={() => setConfirmDelete(false)} disabled={deleting}>{i18nT('pages.schedulePage.cancel')}</Btn>
              <Btn danger disabled={deleting} onClick={async () => { try { setDeleteError(null); setDeleting(true); await api.deleteCron(job.id); onSaved() } catch (e: unknown) { setDeleteError(e instanceof Error ? e.message : i18nT('pages.schedulePage.delete_failed')) } finally { setDeleting(false) } }}>{deleting ? i18nT('pages.schedulePage.deleting_2') : i18nT('pages.schedulePage.delete')}</Btn>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </DialogContent>
  )
}
