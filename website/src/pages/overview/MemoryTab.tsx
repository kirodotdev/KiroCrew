import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from 'react'
import { Trans } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { XCircle, CheckCircle, RefreshCw, Hourglass, Check, BookOpen, SlidersHorizontal } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, EmptyState, Skeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import SimpleSelect from '../../components/SimpleSelect'
import { esc } from '../../api/helpers'
import { findReport, parseErrorCode, parseErrorField } from '../../utils/errorReport'
import VectorMemoryCard from './VectorMemoryCard'
import EmbeddingModelCard from './EmbeddingModelCard'
import MemoryStoreCard, {
  MEMORY_QUERY_PREFIXES,
  MemoryScopeNotice,
  useMemoryStores,
} from './MemoryStoreCard'
import MemoryCarveCard from './MemoryCarveCard'
import MemoryRetiredCard from './MemoryRetiredCard'
import MemoryBackupsCard from './MemoryBackupsCard'
import MemberMemoryPanel from './MemberMemoryPanel'
import MemoryRecordsEditor from './MemoryRecordsEditor'
import MemoryDocCard from './MemoryDocCard'
import Modal from '../../components/Modal'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { useSidePanelLeaveGuard } from '../../components/SidePanelLayout'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import type { Lesson, SessionInfo } from '../../types'
import { useSortableTable } from '../../hooks/useSortableTable'
import SortableHeader from '../../components/SortableHeader'

import { i18nT } from '../../i18n/t'
import { compareText, fmtDateTimeNumeric } from '../../i18n/format'

/** `POST /api/memory/consolidate`'s code for a target the memory modes promise
 *  leaves no durable trace (a Temporary or Incognito session). "Summarize now"
 *  posts every stem `api.sessions` lists, those sessions included, and a row's
 *  `memory_mode` cannot pre-filter them all: a channel thread flagged before its
 *  transcript header carried the mode holds the flag in the session map alone,
 *  which the list is not built from. So the tally sorts the refusals after the
 *  fact -- a skip the user asked for by choosing the mode, counted apart from a
 *  request that failed. */
const RESTRICTED_TARGET_CODE = 'restricted_target_session'

/** Machine-readable fields of a rejected consolidate call's JSON body: the
 *  backend `code`, and for a refused target the `mode` it was in -- both read
 *  through the error journal's own body parser (`parseErrorCode` /
 *  `parseErrorField`), the one parse of a backend error envelope. Duck-typed
 *  on `body` rather than `instanceof ApiError` (the `MobileConnectModal`
 *  shape), so it keeps working under a mocked `api/client`. */
const consolidateRefusal = (reason: unknown): { code?: string; mode?: string } => {
  const body = typeof reason === 'object' && reason !== null && 'body' in reason && typeof reason.body === 'string'
    ? reason.body
    : undefined
  return { code: parseErrorCode(body), mode: parseErrorField(body, 'mode') }
}

/** The mode a skipped session was in, as the tally names it when every skipped
 *  session shares one ("1 skipped: incognito session"): the reader could not tell
 *  whether "temporary or incognito" was two kinds of private chat or one thing
 *  with two names. A literal map, indexed, so every key stays greppable. */
const SKIPPED_MODE_KEYS = {
  incognito: 'pages.overview.memoryTab.skipped_incognito_session',
  temporary: 'pages.overview.memoryTab.skipped_temporary_session',
} as const

/** The Scope cell. The three values are the three delete selectors the list
 *  reports, and each must read differently: a fragment is that scope's row;
 *  `""` is the global row, labelled rather than left blank so it does not read
 *  as missing data beside a scoped sibling; `null` is a row whose stored scope
 *  the store cannot use, labelled so the reader can see that its Delete is the
 *  one that reaches every scope. */
function scopeCell(lesson: Lesson) {
  const scope = lesson.repo_scope
  const repo = scope === null
    ? <span className="text-muted italic" title={i18nT('pages.overview.memoryTab.scope_unusable_hint')}>{i18nT('pages.overview.memoryTab.scope_unusable')}</span>
    : !scope
      ? <span className="text-muted">{i18nT('pages.overview.memoryTab.scope_global')}</span>
      : <span className="font-mono break-all">{scope}</span>
  // The JSONL tier is the other half of the row's identity: a same-text row in
  // the active workspace's file and one in the global file would otherwise read
  // alike, and their Deletes go to different files.
  if (lesson.scope !== 'workspace' || !lesson.workspace) return repo
  return <>{repo}<span className="block text-[12px] text-muted">{i18nT('pages.overview.memoryTab.scope_workspace', { name: lesson.workspace })}</span></>
}

export default function MemoryTab({ refreshTrigger, selectedStore, onStoreNavigate }: { refreshTrigger: number; selectedStore?: string; onStoreNavigate?: (store: string) => void }) {
  const stores = useMemoryStores()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const queryClient = useQueryClient()
  useEffect(() => {
    if (refreshTrigger) for (const prefix of MEMORY_QUERY_PREFIXES) void queryClient.invalidateQueries({ queryKey: prefix })
  }, [refreshTrigger, queryClient])
  const [store, setStore] = useState(() => {
    const selected = new URLSearchParams(window.location.search).get('store') || ''
    return selected === 'default' ? '' : selected
  })
  useEffect(() => {
    if (selectedStore !== undefined) setStore(selectedStore === 'default' ? '' : selectedStore)
  }, [selectedStore])
  const [dirty, setDirty] = useState(false)
  useSidePanelLeaveGuard(() => !dirty || window.confirm(i18nT('memoryV2.leave_discard_explanation')), dirty)
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const [pendingStore, setPendingStore] = useState<string | null>(null)
  const selected = stores.data?.stores.find(s => s.name === store)
  // Content may already be cached while its catalog is still loading. Never
  // turn a private directory name into a member identity, or expose actions
  // until the authoritative owner row arrives. React Query retains settled
  // catalog data during refresh, so existing editors keep their drafts mounted.
  const identityReady = !!selected && (
    selected.is_default
    || (!selected.owner_member && (selected.memory_version === 1 || (selected.memory_version == null && selected.lineage === 'v1')))
    || (selected.memory_version === 2 && !!selected.owner_member)
  )
  const applyStore = (next: string) => {
    setStore(next)
    if (onStoreNavigate) { onStoreNavigate(next); return }
    const url = new URL(window.location.href)
    if (next) url.searchParams.set('store', next)
    else url.searchParams.delete('store')
    window.history.replaceState(window.history.state, '', url)
  }
  const choose = (next: string) => {
    if (next === store) return
    if (dirty) setPendingStore(next)
    else applyStore(next)
  }
  return <>
    <MemoryStoreCard store={store} onStoreChange={choose} compact={!!store} />
    {store ? identityReady ? <MemberMemoryPanel key={store} store={store} summary={selected!} onDirtyChange={setDirty} /> : <Card>
      {stores.isPending ? <div role="status" aria-busy="true" className="flex min-w-0 items-center gap-3">
        <Skeleton className="h-12 w-12 shrink-0 motion-reduce:animate-none" />
        <div className="min-w-0 flex-1 space-y-2">
          <p className="text-[13px] text-muted">{i18nT('memoryV2.identity_loading')}</p>
          <Skeleton className="h-4 w-48 max-w-full motion-reduce:animate-none" />
          <Skeleton className="h-3 w-32 max-w-full motion-reduce:animate-none" />
        </div>
      </div> : <>
        <CardTitle>{i18nT('memoryV2.identity_unavailable')}</CardTitle>
        <MemoryScopeNotice error={stores.error} />
        <div className="flex flex-wrap gap-2">
          <Btn className="min-h-11" disabled={stores.isFetching} onClick={() => void stores.refetch()}>{i18nT('memoryV2.retry_identity')}</Btn>
          <Btn className="min-h-11" onClick={() => {
            const destination = selected?.owner_member
              ? `/capabilities?tab=crews&crew=${encodeURIComponent(selected.owner_member)}`
              : '/capabilities?tab=crews'
            leave(() => navigate(destination), destination)
          }}>{i18nT('pages.kiroCrewAgentsPage.open_crew_manager')}</Btn>
        </div>
      </>}
    </Card> : <GlobalMemoryTab refreshTrigger={refreshTrigger} onDirtyChange={setDirty} />}
    {pendingStore !== null && <Modal open title={i18nT('memoryV2.discard_title')} onClose={() => setPendingStore(null)}><div className="flex flex-col gap-3">
      <p className="text-[13px]">{i18nT('memoryV2.discard_explanation')}</p>
      <div className="flex flex-wrap gap-2">
        <Btn onClick={() => setPendingStore(null)}>{i18nT('pages.kiroCrewAgentsPage.keep_editing')}</Btn>
        <Btn danger onClick={() => { applyStore(pendingStore); setPendingStore(null); setDirty(false) }}>{i18nT('memoryV2.discard_title')}</Btn>
      </div>
    </div></Modal>}
  </>
}

function GlobalMemoryTab({ refreshTrigger, onDirtyChange }: { refreshTrigger: number; onDirtyChange?: (dirty: boolean) => void }) {
  const queryClient = useQueryClient()
  // The failed-session link leaves this tab; through the same guard as the
  // outer tab's exits, since the drafts held here are what the guard protects.
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const [recordDirty, setRecordDirty] = useState(false)
  const [recordsOpen, setRecordsOpen] = useState(false)
  const [docDirty, setDocDirty] = useState<Record<string, boolean>>({})
  const dirty = recordDirty || Object.values(docDirty).some(Boolean)
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
  /** The memory store every store-aware card on this page reads, ON THE WIRE.
   *
   *  `''` means no store is NAMED, which the gateway resolves to the global store —
   *  what every one of these routes served before the picker existed. It is
   *  deliberately not spelled `'default'`: the parameter's PRESENCE is what takes
   *  the owner gate, so naming the store the page already reads would gate a read
   *  that needs no gate and refuse the whole page on an install with no configured
   *  owner. `MemoryStoreCard` displays the active store while this stays `''`. */
  const store = ''
  const stores = useMemoryStores()
  /** Look the row up under the store being SHOWN, not the wire value: `''` matches
   *  no row, so keying on it would make every "is this store readable" answer
   *  default to yes for the store the page is actually displaying. */
  const shownStore = store || stores.data?.active || ''
  const selectedStore = stores.data?.stores.find(s => s.name === shownStore)
  /** A store whose file could not be read has nothing to list. Backups are the
   *  exception and stay visible: a missing database is exactly when a restore is
   *  the thing the operator came for. */
  const storeReadable = selectedStore?.exists !== false

  const [lessons, setLessons] = useState<Lesson[]>([]); const [rule, setRule] = useState(''); const [cat, setCat] = useState('knowledge')
  const [lessonFeedback, setLessonFeedback] = useState<{
    tone: 'info' | 'warning' | 'error'
    text: string
  } | null>(null)
  const [idleHours, setIdleHours] = useState(3); const [maxDays, setMaxDays] = useState(90); const [settingsSaved, setSettingsSaved] = useState(false)
  const [migrated, setMigrated] = useState(false)
  const [vectorActive, setVectorActive] = useState(false)
  const [consolidating, setConsolidating] = useState(false)
  const [consolidateMsg, setConsolidateMsg] = useState<ReactNode>('')
  const [consolidateOk, setConsolidateOk] = useState(false)
  // A failed press, apart from the status message: it reports rejected
  // requests, so it renders through ErrorNotice like every other failure.
  // `session` is the first failed request's session key, `count` how many failed.
  // One failure notice for the press's two request kinds: the session LIST
  // (no session, no tally -- `session` absent) and a per-session request (the
  // first failed one named, `count` failed in all). `summary` is the plain-words
  // line for that kind; `raw` is the server's own reply, kept verbatim.
  const [consolidateFailure, setConsolidateFailure] = useState<{ title: string; summary: string; raw: string; session?: { key: string; title?: string }; count: number } | null>(null)
  // Track all "Saved" / "consolidate-msg-clear" timeout ids so they can be
  // cleared on unmount — otherwise a pending setTimeout fires after the
  // component is gone and (in vitest) shows up as an unhandled error from
  // "tasks running past test environment teardown".
  const timeoutsRef = useRef<ReturnType<typeof setTimeout>[]>([])
  useEffect(() => () => {
    timeoutsRef.current.forEach(clearTimeout)
    timeoutsRef.current = []
  }, [])
  const scheduleClear = useCallback((fn: () => void, ms: number) => {
    const id = setTimeout(() => {
      timeoutsRef.current = timeoutsRef.current.filter(t => t !== id)
      fn()
    }, ms)
    timeoutsRef.current.push(id)
    return id
  }, [])
  // The tally's own clear timer, so a press that lands within four seconds of
  // the previous one cancels that one's clear instead of losing its tally to it.
  const consolidateClearRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const loadLessons = useCallback(async () => { const d = await api.lessons(); setLessons(d.lessons || []) }, [])
  const { confirm, confirmDialog } = useConfirm()
  // Which step of a delete failed decides the banner's title: the request
  // itself (the row is still stored) or the list refresh after it succeeded
  // (the row is gone but may still be shown).
  const [deleteError, setDeleteError] = useState<{ step: 'delete' | 'refresh' | 'nothing'; message?: string } | null>(null)
  // A `null` scope is the one row whose Delete cannot be limited to itself: the
  // route refuses the stored value as a selector, so the client sends none and
  // the unselective delete removes every same-rule row in every scope. That is
  // the collateral this tab otherwise exists to prevent, so it asks first --
  // through the shared themed dialog, whose confirm button restates the act.
  const deleteLesson = async (l: Lesson) => {
    if (l.repo_scope === null && !(await confirm({
      title: i18nT('pages.overview.memoryTab.delete_unusable_scope_title'),
      body: i18nT('pages.overview.memoryTab.delete_unusable_scope_confirm'),
      confirmLabel: i18nT('pages.overview.memoryTab.delete_unusable_scope_button'),
    }))) return
    setDeleteError(null)
    // Both steps are awaited and reported where the row is, rather than letting
    // the click end in silence: a rejected delete leaves the row stored, and a
    // rejected refresh leaves a deleted row on screen.
    // `exact`: this row holds the whole rule, so it names exactly one row; the
    // route's default substring match would also take every longer rule that
    // contains it.
    let result: { ok: boolean }
    try {
      result = await api.deleteLesson(l.rule, l.repo_scope, { scope: l.scope, workspace: l.workspace, exact: true })
    } catch (e) {
      setDeleteError({ step: 'delete', message: e instanceof Error ? e.message : String(e) })
      return
    }
    if (!result?.ok) {
      // The store found no row matching these selectors: the list is stale, or
      // the displayed (redacted) text differs from the stored one.
      setDeleteError({ step: 'nothing' })
    }
    try {
      await loadLessons()
    } catch (e) {
      setDeleteError({ step: 'refresh', message: e instanceof Error ? e.message : String(e) })
    }
  }
  const lessonComparators = useMemo(() => ({
    rule: (a: Lesson, b: Lesson) => a.rule.localeCompare(b.rule),
    category: (a: Lesson, b: Lesson) => a.category.localeCompare(b.category),
    repo_scope: (a: Lesson, b: Lesson) => compareText(a.repo_scope ?? '', b.repo_scope ?? ''),
    ts: (a: Lesson, b: Lesson) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
  }), [])
  const recentLessons = useMemo(() => lessons.slice(-20), [lessons])
  const { sorted: sortedLessons, sort: lessonSort, toggle: toggleLessonSort } = useSortableTable(recentLessons, 'memory-lessons', lessonComparators, { key: 'ts', dir: 'desc' })
  useEffect(() => {
    api.memorySettings().then(d => { setIdleHours(d.history_idle_hours ?? 3); setMaxDays(d.history_max_days ?? 90); setMigrated(d.migrated ?? false) })
    loadLessons()
  }, [loadLessons])
  // The page's own refresh signal. Invalidated by query-key PREFIX rather than
  // for the selected store only, so the rows cached for a store the user looked
  // at earlier cannot outlive the refresh and reappear on the next switch.
  useEffect(() => {
    loadLessons()
    for (const prefix of MEMORY_QUERY_PREFIXES) {
      queryClient.invalidateQueries({ queryKey: prefix })
    }
  }, [refreshTrigger, loadLessons, queryClient])
  const consolidate = async () => {
    if (consolidateClearRef.current !== null) {
      clearTimeout(consolidateClearRef.current)
      timeoutsRef.current = timeoutsRef.current.filter(t => t !== consolidateClearRef.current)
      consolidateClearRef.current = null
    }
    setConsolidating(true); setConsolidateMsg(''); setConsolidateOk(false); setConsolidateFailure(null)
    // The list is the press's first request. A gateway that is down or answers
    // 500 is a FAILURE of the press, not an empty list: it renders through the
    // same notice as a failed per-session request, with the server's reply as
    // the detail -- swallowed into `sessions: []`, it read as "start a chat
    // first" with no error surface at all. That message is for a list that
    // succeeded with nothing in it.
    let sessions: { sessions?: SessionInfo[] } | undefined
    try {
      sessions = await api.sessions(200)
    } catch (err) {
      setConsolidateFailure({
        title: i18nT('pages.overview.memoryTab.consolidate_list_failed'),
        summary: i18nT('pages.overview.memoryTab.consolidate_list_failed_summary'),
        raw: err instanceof Error ? err.message : String(err),
        count: 0,
      })
      setConsolidating(false)
      return
    }
    const listed = sessions?.sessions?.filter((s: SessionInfo) => Boolean(s.key)) || []
    const keys = listed.map((s: SessionInfo) => s.key)
    if (keys.length === 0) { setConsolidateMsg(<><XCircle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.no_sessions_to_consolidate_start_a_chat_first')}</>); setConsolidating(false); return }
    const results = await Promise.allSettled(keys.map((k: string) => api.consolidateMemory(k, true)))
    const succeeded = results.filter(r => r.status === 'fulfilled').length
    // Sort every rejection: a refusal the route names by its code is a skip the
    // user asked for by choosing the mode, counted apart from a request that
    // failed -- and BY MODE, so the tally can name the one mode every skip
    // shares, or count each mode when they differ.
    const byMode = { temporary: 0, incognito: 0 }
    let unnamed = 0
    let skipped = 0
    let failed = 0
    results.forEach(r => {
      if (r.status !== 'rejected') return
      const refusal = consolidateRefusal(r.reason)
      if (refusal.code !== RESTRICTED_TARGET_CODE) { failed += 1; return }
      skipped += 1
      if (refusal.mode === 'temporary' || refusal.mode === 'incognito') byMode[refusal.mode] += 1
      else unnamed += 1
    })
    // The one mode every skipped session was in, or nothing.
    const onlyMode = skipped > 0 && byMode.temporary === skipped ? 'temporary'
      : skipped > 0 && byMode.incognito === skipped ? 'incognito'
        : undefined
    const mode = onlyMode ? i18nT(SKIPPED_MODE_KEYS[onlyMode], { count: skipped }) : undefined
    // The parenthetical of the mixed tally: the count of each mode ("1
    // temporary, 1 incognito" -- the reader could not tell what made a session
    // one or the other, or which of theirs were which; the counts at least say
    // how many of each), or the either/or wording when a body named no mode.
    const modes = unnamed === 0
      ? i18nT('pages.overview.memoryTab.skipped_modes_breakdown', {
        temporary: i18nT('pages.overview.memoryTab.skipped_temporary_count', { count: byMode.temporary }),
        incognito: i18nT('pages.overview.memoryTab.skipped_incognito_count', { count: byMode.incognito }),
      })
      : i18nT('pages.overview.memoryTab.skipped_modes_unknown')
    // `count` drives the mixed keys' plural ("1 private session skipped").
    const tally = { succeeded, total: keys.length, failed, skipped, mode, modes, count: skipped }
    // The skip fragment as it reads on its own: the mode when every skip shares
    // one, "N private sessions skipped (1 temporary, 1 incognito)" otherwise --
    // the category leads and the per-mode counts are the parenthetical.
    const skippedFragment = skipped > 0
      ? (mode
        ? i18nT('pages.overview.memoryTab.skipped_sessions_mode', tally)
        : i18nT('pages.overview.memoryTab.skipped_sessions', tally))
      : undefined
    if (failed > 0) {
      // A failed request is an error, so it renders through ErrorNotice: the
      // tally is the lead, a plain-words line says what happened and what to
      // do (press the button again), and the footer names the FIRST failed
      // request's session key beside that request's own string -- kept raw
      // rather than localized, as the rule asks (the string is the journal key,
      // should the hand-off ever be turned on here); with several failures the
      // count is in the lead and this is the first of them. The skip fragment
      // is NOT part of the notice: it is the other, successful half of the
      // press, so it renders as the success-tone tally BESIDE the notice --
      // inside the danger box, even in its own colour, the reader could not
      // tell whether the skip was part of the problem. Both persist until the
      // notice is dismissed: a failure the user has to act on must not vanish
      // on the success tally's timer, and the skip belongs to the same press.
      const firstIndex = results.findIndex(r => r.status === 'rejected' && consolidateRefusal(r.reason).code !== RESTRICTED_TARGET_CODE)
      const first = results[firstIndex]
      const reason = first && first.status === 'rejected' ? first.reason : undefined
      setConsolidateFailure({
        title: i18nT('pages.overview.memoryTab.consolidated_sessions_failed', tally),
        summary: i18nT('pages.overview.memoryTab.consolidate_failed_summary'),
        raw: reason instanceof Error ? reason.message : String(reason),
        session: { key: keys[firstIndex] ?? '', title: listed[firstIndex]?.title },
        count: failed,
      })
      if (skippedFragment) { setConsolidateMsg(<><CheckCircle className="lucide-inline" /> {skippedFragment}</>); setConsolidateOk(true) }
    } else if (skipped > 0) {
      setConsolidateMsg(<><CheckCircle className="lucide-inline" /> {mode
        ? i18nT('pages.overview.memoryTab.consolidated_sessions_skipped_mode', tally)
        : i18nT('pages.overview.memoryTab.consolidated_sessions_skipped', tally)}</>); setConsolidateOk(true)
    } else {
      setConsolidateMsg(<><CheckCircle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.consolidated')} {i18nT('pages.overview.memoryTab.session', { count: succeeded })}</>); setConsolidateOk(true)
    }
    setConsolidating(false)
    if (failed === 0) consolidateClearRef.current = scheduleClear(() => { consolidateClearRef.current = null; setConsolidateMsg('') }, 4000)
  }
  const addLesson = async () => {
    if (!rule) return
    setLessonFeedback(null)
    const result = await api.createLesson(rule, cat)
    if (result.outcome === 'inserted' || result.outcome === 'enriched') {
      setRule('')
      await loadLessons()
      return
    }
    if (result.outcome === 'unchanged') {
      setRule('')
      setLessonFeedback({
        tone: 'info',
        text: i18nT('pages.overview.memoryTab.lesson_already_stored'),
      })
      return
    }
    if (result.outcome === 'deduped') {
      setLessonFeedback({
        tone: 'warning',
        text: i18nT('pages.overview.memoryTab.lesson_already_covered', {
          reason: result.reason,
        }),
      })
      return
    }
    setLessonFeedback({
      tone: 'error',
      text: i18nT('pages.overview.memoryTab.lesson_not_saved', {
        reason: result.reason,
      }),
    })
  }
  return (<>
    <Card><CardTitle>{i18nT('pages.overview.memoryTab.memory_settings')} <InfoTip text={i18nT('pages.overview.memoryTab.controls_how_conversation_history_is_consolidate')} /></CardTitle>
      <div className="flex gap-3 items-end flex-wrap">
        <label htmlFor="memory-idle-hours" className="flex flex-col gap-1 text-[13px] text-muted">
          <span>{i18nT('pages.overview.memoryTab.consolidation_idle_hours')}</span>
          <input id="memory-idle-hours" aria-label={i18nT('pages.overview.memoryTab.consolidation_idle_hours')} type="number" min={0.5} max={24} step={0.5} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-hidden transition-colors focus-ring" value={idleHours} onChange={e => setIdleHours(Number(e.target.value))} />
        </label>
        {!migrated && (
          <label htmlFor="memory-max-days" className="flex flex-col gap-1 text-[13px] text-muted">
            <span>{i18nT('pages.overview.memoryTab.history_retention_days')}</span>
            <input id="memory-max-days" aria-label={i18nT('pages.overview.memoryTab.history_retention_days')} type="number" min={7} max={365} step={1} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-hidden transition-colors focus-ring" value={maxDays} onChange={e => setMaxDays(Number(e.target.value))} />
          </label>
        )}
        <Btn onClick={async () => { await api.saveMemorySettings({ history_idle_hours: idleHours, history_max_days: maxDays }); setSettingsSaved(true); scheduleClear(() => setSettingsSaved(false), 2000) }}>{settingsSaved ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}</Btn>
        <Btn onClick={consolidate} disabled={consolidating}>{consolidating ? <><Hourglass className="lucide-inline" /> {i18nT('pages.overview.memoryTab.running')}</> : <><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryTab.summarize_now')}</>}</Btn>
        {/* What the button does, under it on its own line: the blind reader
            hesitated over "summarize WHAT" without it and pressed with it (the
            UX lane's read of two heads). Names the button rather than saying
            "it", since the line sits under the whole row. */}
        <p className="basis-full m-0 text-[12px] text-muted" data-testid="summarize-now-help">{i18nT('pages.overview.memoryTab.summarize_now_help')}</p>
        {/* No hand-off: the tab holds unsaved drafts -- the lesson rule being
            typed below and the store text in the editors -- that the hand-off's
            navigation would discard. The block variant on its own flex line
            (`basis-full`) so the button row stays a row. */}
        {consolidateFailure && (
          <ErrorNotice
            className="basis-full"
            title={consolidateFailure.title}
            /* Plain words a person can act on -- what happened, and that pressing
               the button again retries it; the server's raw string is the journal
               key the structured report is looked up by, so it rides as `report`
               and, verbatim, as the footer's secondary detail under the failed
               session's key. The detail carries a visible label ("Server's
               reply:") so the message's "shown below" unambiguously points at it
               and marks it as foreign text -- the backend still says
               "consolidat*" where every label here says "Summarize", and unlabelled
               the reader doubted it concerned the button they pressed. */
            message={consolidateFailure.summary}
            report={findReport(consolidateFailure.raw)}
            footer={<>
              {consolidateFailure.session !== undefined && (
                /* The session by its TITLE, as the sidebar names it (the key only when
                   it has none), and a way to act on it: the same guarded navigation as
                   the tab's other exits, since a click leaves the drafts this tab holds. */
                <span className="block text-[12px]" data-testid="consolidate-failed-session">
                  <Trans
                    i18nKey="pages.overview.memoryTab.consolidate_failed_session"
                    count={consolidateFailure.count}
                    values={{ session: consolidateFailure.session.title || consolidateFailure.session.key }}
                    // eslint-disable-next-line jsx-a11y/control-has-associated-label -- the control's label is the session title `Trans` renders inside it
                    components={{ session: <button
                      type="button"
                      className="p-0 border-none bg-transparent font-body text-[12px] text-inherit underline underline-offset-2 cursor-pointer hover:text-accent"
                      data-testid="consolidate-failed-session-link"
                      onClick={() => {
                        const destination = `/chat?sid=${encodeURIComponent(consolidateFailure.session?.key ?? '')}`
                        leave(() => navigate(destination), destination)
                      }}
                    /> }}
                  />
                </span>
              )}
              <span className="block text-[12px] text-muted" data-testid="consolidate-failed-reply">
                <Trans
                  i18nKey="pages.overview.memoryTab.consolidate_failed_reply"
                  values={{ reply: consolidateFailure.raw }}
                  components={{ reply: <span className="font-mono" data-testid="consolidate-failed-detail" /> }}
                />
              </span>
            </>}
            onDismiss={() => { setConsolidateFailure(null); setConsolidateMsg('') }}
            askAgent={false}
            testId="consolidate-failed"
          />
        )}
        {consolidateMsg && <span className={`text-[13px] ${consolidateOk ? 'text-ok' : 'text-danger'}`} data-testid="consolidate-msg">{consolidateMsg}</span>}

        {migrated && <span className="text-[12px] text-muted ml-2">{i18nT('pages.overview.memoryTab.semantic_memory_active_text_files_are_read_only')}</span>}
      </div>
    </Card>
    <VectorMemoryCard onActiveChange={setVectorActive} onMigratedChange={setMigrated} />
    <EmbeddingModelCard />
    <details onToggle={event => setRecordsOpen(event.currentTarget.open)}>
      <summary className="min-h-11 cursor-pointer rounded-lg border border-border p-3 text-[13px] text-muted">
        <SlidersHorizontal className="lucide-inline mr-2" aria-hidden="true" />
        {i18nT('memoryV2.edit_saved_memories')}
      </summary>
      {recordsOpen && <div className="mt-4"><MemoryRecordsEditor store="default" onDirtyChange={setRecordDirty} /></div>}
    </details>
    {/* The graph explorer lives on Developer. This user-facing V1 browser keeps
        its preferences, projects, history, semantic/episodic records, recovery,
        settings and lessons in the normal page flow. */}
    {/* `vectorActive` comes from the vector card, which reads the global store,
        independently of the picked one — so these three text documents are hidden whenever THAT
        store has migrated to semantic memory, whichever store the picker names.
        Pre-existing coupling, kept rather than widened: making the gate per-store
        needs the vector card to take the picker too, and that card is where the
        migration state is actually known. */}
    {!vectorActive && (<>
      {/* `key={store}`: a remount is what drops an unsaved draft when the scope
          changes, so a body typed against one store can never be saved into
          another. */}
      <MemoryDocCard
        key={`preferences-${store}`}
        docKey="preferences"
        onDirtyChange={dirty => setDocDirty(old => old.preferences === dirty ? old : { ...old, preferences: dirty })}
        store={store}
        title={i18nT('pages.overview.memoryTab.preferences')}
        info={i18nT('pages.overview.memoryTab.learned_user_preferences_coding_style_tools_work')}
        rows={8}
        placeholder={i18nT('pages.overview.memoryTab.loading')}
        read={s => api.memoryPreferences(s)}
        write={(c, s) => api.saveMemoryPreferences(c, s)}
      />
      <MemoryDocCard
        key={`projects-${store}`}
        docKey="projects"
        onDirtyChange={dirty => setDocDirty(old => old.projects === dirty ? old : { ...old, projects: dirty })}
        store={store}
        title={i18nT('pages.overview.memoryTab.projects')}
        rows={8}
        placeholder={i18nT('pages.overview.memoryTab.loading')}
        read={s => api.memoryProjects(s)}
        write={(c, s) => api.saveMemoryProjects(c, s)}
      />
      <MemoryDocCard
        key={`history-${store}`}
        docKey="history"
        onDirtyChange={dirty => setDocDirty(old => old.history === dirty ? old : { ...old, history: dirty })}
        store={store}
        title={i18nT('pages.overview.memoryTab.daily_history')}
        rows={10}
        mono
        placeholder={i18nT('pages.overview.memoryTab.no_history_yet')}
        read={s => api.memoryHistory(s)}
        write={(c, s) => api.saveMemoryHistory(c, s)}
      />
    </>)}
    {/* Store-specific keys REMOUNT each card on a store switch, which discards
        its per-store local state. Without it, MemoryBackupsCard keeps an ARMED
        restore across the switch — and a backup's name is not unique across stores
        (one sweep stamps every store's copy identically, and every store's file
        stem is `memory`), so the same-named row of the newly picked store renders
        already-confirmed and one click restores a store the operator never armed.
        Its "Back up now" and "Restored" status lines have the same problem in a
        milder form: they would report a mutation that landed on the store the card
        no longer shows. */}
    {storeReadable && <MemoryCarveCard key={`carve-${store}`} store={store} />}
    {storeReadable && <MemoryRetiredCard key={`retired-${store}`} store={store} />}
    <MemoryBackupsCard key={`backups-${store}`} store={store} />
    {!vectorActive && (
      <Card><CardTitle>{i18nT('pages.overview.memoryTab.lessons')} <InfoTip text={i18nT('pages.overview.memoryTab.persistent_lessons_injected_into_every_session_a')} /></CardTitle>
      <div className="flex gap-2 items-center flex-wrap mb-3">
        <Input placeholder={i18nT('pages.overview.memoryTab.rule_e_g_always_use_tabs_not_spaces')} style={{ flex: 2 }} value={rule} onChange={e => setRule(e.target.value)} />
        <SimpleSelect
          aria-label={i18nT('pages.overview.memoryTab.category')}
          style={{ flex: '0 0 140px' }}
          options={['knowledge', 'tool', 'preference']}
          optionLabels={[i18nT('pages.overview.memoryTab.knowledge'), i18nT('pages.overview.memoryTab.tool'), i18nT('pages.overview.memoryTab.preference')]}
          value={cat}
          onChange={setCat}
        />
        <SendBtn onClick={addLesson}>{i18nT('pages.overview.memoryTab.add')}</SendBtn>
        {lessonFeedback && (
          <span
            role={lessonFeedback.tone === 'error' ? 'alert' : 'status'}
            className={`text-[13px] ${
              lessonFeedback.tone === 'error'
                ? 'text-danger'
                : lessonFeedback.tone === 'warning'
                  ? 'text-warn'
                  : 'text-muted'
            }`}
          >
            {lessonFeedback.text}
          </span>
        )}
      </div>
      {/* No agent hand-off: it navigates away, and the Add row above may hold
          an unsaved rule draft. */}
      <ErrorNotice
        title={i18nT(deleteError?.step === 'refresh' ? 'pages.overview.memoryTab.lessons_refresh_failed' : 'pages.overview.memoryTab.delete_failed')}
        message={deleteError?.step === 'nothing' ? i18nT('pages.overview.memoryTab.delete_matched_nothing') : deleteError?.message}
        onDismiss={() => setDeleteError(null)}
        askAgent={false}
        className="mb-2"
      />
      {/* Scrolls sideways rather than clipping: five columns plus a long path
          fragment overrun a narrow viewport, and the card hides overflow. */}
      <div className="overflow-x-auto"><table className="w-full border-collapse table-striped"><thead><tr><SortableHeader label={i18nT('pages.overview.memoryTab.rule')} sortKey="rule" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.category')} sortKey="category" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.scope')} sortKey="repo_scope" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.when')} sortKey="ts" sort={lessonSort} onToggle={toggleLessonSort} /><th aria-label={i18nT('pages.overview.memoryTab.actions')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium"></th></tr></thead>
        <tbody>{lessons.length === 0 ? <tr><td colSpan={5}><EmptyState icon={<BookOpen className="lucide-inline" />} title={i18nT('pages.overview.memoryTab.no_lessons_yet')} subtitle={i18nT('pages.overview.memoryTab.lessons_empty_subtitle')} /></td></tr> : sortedLessons.map((l) => (
          // Scope is part of the key: a scoped and a global row sharing rule text
          // are two lessons, and can share a timestamp. String() keeps the null
          // (unusable-scope) row distinct from the "" (global) one; the JSONL tier
          // keeps a workspace row distinct from a global one.
          <tr key={`${l.rule}-${String(l.repo_scope)}-${l.workspace ?? ''}-${l.ts}`} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm">{esc(l.rule)}</td><td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="ok">{l.category}</Badge></td><td className="px-2.5 py-2 border-b border-border text-sm">{scopeCell(l)}</td><td className="px-2.5 py-2 border-b border-border text-sm">{fmtDateTimeNumeric(l.ts)}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm"><Btn danger onClick={() => deleteLesson(l)}>{i18nT('pages.overview.memoryTab.delete')}</Btn></td></tr>
        ))}</tbody></table></div></Card>
    )}
    {confirmDialog}
  </>)
}
