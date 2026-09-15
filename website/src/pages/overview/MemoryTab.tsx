import { useState, useEffect, useCallback, useMemo, useRef, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { XCircle, AlertTriangle, CheckCircle, RefreshCw, Hourglass, Check, BookOpen, SlidersHorizontal } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, EmptyState, Skeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import SimpleSelect from '../../components/SimpleSelect'
import { esc } from '../../api/helpers'
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
import { useSidePanelLeaveGuard } from '../../components/SidePanelLayout'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import type { Lesson, SessionInfo } from '../../types'
import { useSortableTable } from '../../hooks/useSortableTable'
import SortableHeader from '../../components/SortableHeader'

import { i18nT } from '../../i18n/t'
import { fmtDateTimeNumeric } from '../../i18n/format'

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
  }, [])
  const loadLessons = useCallback(async () => { const d = await api.lessons(); setLessons(d.lessons || []) }, [])
  const lessonComparators = useMemo(() => ({
    rule: (a: Lesson, b: Lesson) => a.rule.localeCompare(b.rule),
    category: (a: Lesson, b: Lesson) => a.category.localeCompare(b.category),
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
    setConsolidating(true); setConsolidateMsg(''); setConsolidateOk(false)
    const sessions = await api.sessions(200).catch(() => ({ sessions: [] }))
    const keys = sessions?.sessions?.map((s: SessionInfo) => s.key).filter(Boolean) || []
    if (keys.length === 0) { setConsolidateMsg(<><XCircle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.no_sessions_to_consolidate_start_a_chat_first')}</>); setConsolidating(false); return }
    const results = await Promise.allSettled(keys.map((k: string) => api.consolidateMemory(k, true)))
    const succeeded = results.filter(r => r.status === 'fulfilled').length
    const failed = results.filter(r => r.status === 'rejected').length
    if (failed > 0) setConsolidateMsg(<><AlertTriangle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.consolidated_sessions_failed', { succeeded, total: keys.length, failed })}</>)
    else { setConsolidateMsg(<><CheckCircle className="lucide-inline" /> {i18nT('pages.overview.memoryTab.consolidated')} {i18nT('pages.overview.memoryTab.session', { count: succeeded })}</>); setConsolidateOk(true) }
    setConsolidating(false)
    scheduleClear(() => setConsolidateMsg(''), 4000)
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
          <input id="memory-idle-hours" aria-label={i18nT('pages.overview.memoryTab.consolidation_idle_hours')} type="number" min={0.5} max={24} step={0.5} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none transition-colors focus-ring" value={idleHours} onChange={e => setIdleHours(Number(e.target.value))} />
        </label>
        {!migrated && (
          <label htmlFor="memory-max-days" className="flex flex-col gap-1 text-[13px] text-muted">
            <span>{i18nT('pages.overview.memoryTab.history_retention_days')}</span>
            <input id="memory-max-days" aria-label={i18nT('pages.overview.memoryTab.history_retention_days')} type="number" min={7} max={365} step={1} className="w-24 bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none transition-colors focus-ring" value={maxDays} onChange={e => setMaxDays(Number(e.target.value))} />
          </label>
        )}
        <Btn onClick={async () => { await api.saveMemorySettings({ history_idle_hours: idleHours, history_max_days: maxDays }); setSettingsSaved(true); scheduleClear(() => setSettingsSaved(false), 2000) }}>{settingsSaved ? <><Check className="lucide-inline" /> {i18nT('pages.overview.memoryTab.saved')}</> : i18nT('pages.overview.memoryTab.save')}</Btn>
        <Btn onClick={consolidate} disabled={consolidating}>{consolidating ? <><Hourglass className="lucide-inline" /> {i18nT('pages.overview.memoryTab.running')}</> : <><RefreshCw className="lucide-inline" /> {i18nT('pages.overview.memoryTab.summarize_now')}</>}</Btn>
        {consolidateMsg && <span className={`text-[13px] ${consolidateOk ? 'text-ok' : 'text-danger'}`}>{consolidateMsg}</span>}

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
      <table className="w-full border-collapse table-striped"><thead><tr><SortableHeader label={i18nT('pages.overview.memoryTab.rule')} sortKey="rule" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.category')} sortKey="category" sort={lessonSort} onToggle={toggleLessonSort} /><SortableHeader label={i18nT('pages.overview.memoryTab.when')} sortKey="ts" sort={lessonSort} onToggle={toggleLessonSort} /><th aria-label={i18nT('pages.overview.memoryTab.actions')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium"></th></tr></thead>
        <tbody>{lessons.length === 0 ? <tr><td colSpan={4}><EmptyState icon={<BookOpen className="lucide-inline" />} title={i18nT('pages.overview.memoryTab.no_lessons_yet')} subtitle={i18nT('pages.overview.memoryTab.lessons_empty_subtitle')} /></td></tr> : sortedLessons.map((l) => (
          <tr key={`${l.rule}-${l.ts}`} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm">{esc(l.rule)}</td><td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="ok">{l.category}</Badge></td><td className="px-2.5 py-2 border-b border-border text-sm">{fmtDateTimeNumeric(l.ts)}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm"><Btn danger onClick={async () => { await api.deleteLesson(l.rule); loadLessons() }}>{i18nT('pages.overview.memoryTab.delete')}</Btn></td></tr>
        ))}</tbody></table></Card>
    )}
  </>)
}
