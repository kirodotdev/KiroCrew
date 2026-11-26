import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { Copy, ArrowUpRight } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Input, PanelSectionHeader } from '../../components/ui'
import { MEMORY_RECORD_LABELS, semanticValueText } from './memoryRecordParts'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import SimpleSelect from '../../components/SimpleSelect'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { fmtNumber } from '../../i18n/format'
import type { MemoryStoreSummary } from '../../types'
import { MEMORY_QUERY_PREFIXES, MemoryStoreAvatar, MemoryStoreStats, memoryQueryRetry, useMemoryStores } from './MemoryStoreCard'
import MemoryRetiredCard from './MemoryRetiredCard'
import MemoryBackupsCard from './MemoryBackupsCard'
import MemoryDocCard from './MemoryDocCard'
import MemoryPageShell from './memoryPage/MemoryPageShell'
import MemoryList from './memoryPage/MemoryList'
import { MemoryEmbeddedLine } from './memoryPage/MemoryEmbeddedLine'
import { MemoryGraphPanel } from './memoryPage/MemoryGraphPanel'
import { memoryRoute } from '../settings/memoryRoute'
import { seedInBatches, sourceRows, type SeedCounts } from './memorySeedBatches'
import { PILL_ON } from './memoryPage/MemoryListParts'
import { useIsMobile } from '../../hooks/useIsMobile'

type Kind = 'fact' | 'directive' | 'episode'
type MemoryRow = { key?: string; id?: string; value_json?: unknown; text?: string; source?: string; created_at?: string; derived_from?: string }
type Selection = { kind: Kind; id: string }
// The copy dialog's kind filter, in the memory page's kind order.
const KIND_FILTERS: [Kind, string][] = [['fact', 'memoryV2.facts_label'], ['directive', 'memoryV2.rules_label'], ['episode', 'memoryV2.experiences_label']]
const keyOf = (row: MemoryRow) => row.key || row.id || ''
const kindOf = (row: MemoryRow): Kind => row.key ? (row.key.startsWith('lesson.') ? 'directive' : 'fact') : 'episode'
const storedValue = (row: MemoryRow): unknown => {
  if (typeof row.value_json !== 'string') return row.value_json
  try { return JSON.parse(row.value_json) } catch { return row.value_json }
}
const bodyOf = (row: MemoryRow) => {
  const value = storedValue(row)
  return row.key ? kindOf(row) === 'directive' && value && typeof value === 'object' && 'rule' in value ? String(value.rule) : semanticValueText(row) : row.text || ''
}
const errorText = (error: unknown) => error instanceof Error ? error.message : error ? String(error) : ''

function useMemoryRows(store: string, enabled = true, query = '') {
  const [search, setSearch] = useState(query)
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), 250)
    return () => clearTimeout(timer)
  }, [query])
  const page = (kind: 'semantic' | 'episodic', offset: number) => search ? api.memberMemoryPage(store, kind, offset, search) : api.memberMemoryPage(store, kind, offset)
  const nextPage = (last: { entries: MemoryRow[] }, pages: unknown[]) => last.entries.length === 100 ? pages.length * 100 : undefined
  const facts = useInfiniteQuery({ queryKey: ['member-memory', store, 'facts', search], initialPageParam: 0, queryFn: ({ pageParam }) => page('semantic', pageParam), getNextPageParam: nextPage, enabled, retry: memoryQueryRetry })
  const episodes = useInfiniteQuery({ queryKey: ['member-memory', store, 'episodes', search], initialPageParam: 0, queryFn: ({ pageParam }) => page('episodic', pageParam), getNextPageParam: nextPage, enabled, retry: memoryQueryRetry })
  const rows = useMemo(() => [...(facts.data?.pages || []), ...(episodes.data?.pages || [])].flatMap(page => page.entries || []) as MemoryRow[], [facts.data, episodes.data])
  return { rows, error: facts.error || episodes.error, pending: search !== query.trim() || facts.isPending || episodes.isPending, more: facts.hasNextPage || episodes.hasNextPage, loadingMore: facts.isFetchingNextPage || episodes.isFetchingNextPage,
    loadMore: () => Promise.all([...(facts.hasNextPage ? [facts.fetchNextPage()] : []), ...(episodes.hasNextPage ? [episodes.fetchNextPage()] : [])]) }
}

function SeedMemory({ store, member, onClose, onCopied }: { store: string; member: string; onClose: () => void; onCopied: () => void }) {
  const { t } = useTranslation()
  const stores = useMemoryStores()
  const [source, setSource] = useState('default')
  const [selection, setSelection] = useState<Selection[]>([])
  const [filter, setFilter] = useState('')
  const phone = useIsMobile()
  const data = useMemoryRows(source, true, filter)
  const [counts, setCounts] = useState<SeedCounts | null>(null)
  // One box copies every source row of the picked kinds: all of them ("Copy all")
  // with no search, or those the search matches ("Select all matching"). Rows are
  // read page by page while it copies, so no count is loaded up front and none is capped.
  const [allMatching, setAllMatching] = useState(false)
  const [kinds, setKinds] = useState<ReadonlySet<Kind>>(() => new Set(KIND_FILTERS.map(([kind]) => kind)))
  const searching = !!filter.trim()
  const copy = useMutation({
    mutationFn: () => {
      setCounts({ imported: 0, existing: 0, failed: 0 })
      return seedInBatches(source, store, allMatching ? sourceRows(source, filter.trim(), kinds) : selection, setCounts)
    },
    onSettled: onCopied,
  })
  const failedResults = copy.data?.results.filter(r => ['unconfirmed', 'rejected', 'not_attempted'].includes(r.outcome)) ?? []
  const failureReasons = failedResults.flatMap(result => result.reason?.trim() ? [result.reason.trim()] : [])
  const copyFailure = [
    copy.data?.partial ? t('memoryV2.copy_partial') : '',
    copy.data?.error ? errorText(copy.data.error) : '',
    ...failureReasons,
    failureReasons.length < failedResults.length ? t('appSdk.chatMessageList.copy_failed') : '',
  ].filter(Boolean).join('\n')
  const choices = (stores.data?.stores || []).filter(s => s.name !== store && s.exists)
  const visible = data.pending || data.error ? [] : data.rows.filter(row => kinds.has(kindOf(row)))
  // The last picked kind stays on, so the list is never emptied by the filter alone.
  const toggleKind = (kind: Kind) => {
    const next = new Set(kinds)
    if (next.has(kind)) next.delete(kind); else next.add(kind)
    setKinds(next)
    setSelection(old => old.filter(s => next.has(s.kind)))
  }
  const toggle = (row: MemoryRow) => {
    setAllMatching(false)
    const item = { kind: kindOf(row), id: keyOf(row) }
    setSelection(old => old.some(s => s.id === item.id && s.kind === item.kind) ? old.filter(s => s.id !== item.id || s.kind !== item.kind) : [...old, item])
  }
  const picked = (row: MemoryRow) => allMatching || selection.some(s => s.id === keyOf(row) && s.kind === kindOf(row))
  const tallies = counts && { imported: fmtNumber(counts.imported), existing: fmtNumber(counts.existing), failed: fmtNumber(counts.failed) }
  return <Modal open title={t('memoryV2.copy_dialog_title', { member })} height={phone ? 'calc(100dvh - 2rem)' : undefined} guardAccidentalDismiss={allMatching || selection.length > 0} dismissDisabled={copy.isPending} onClose={onClose} footer={<div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
    {tallies ? <span role="status" className="text-[13px] tabular-nums text-text">{t(counts!.failed ? 'memoryV2.copy_tally_failed' : 'memoryV2.copy_tally', tallies)}</span> : <span />}
    <Btn primary className="min-h-11 justify-center" disabled={copy.isPending || !!data.error || !(allMatching ? visible.length : selection.length)} onClick={() => copy.mutate()}><Copy className="lucide-inline" />{allMatching ? t(searching ? 'memoryV2.copy_all_matching' : 'memoryV2.copy_all') : t('memoryV2.copy_selected', { count: fmtNumber(selection.length) })}</Btn>
  </div>}>
    <div className="flex min-w-0 flex-col gap-3">
      <p className="text-[13px] text-muted">{t('memoryV2.copy_explanation')}</p>
      <SimpleSelect aria-label={t('memoryV2.source')} options={choices.map(s => s.name)} optionLabels={choices.map(s => s.is_default ? t('pages.kiroCrewAgentsPage.global_memory_v1') : s.owner_member || s.name)} optionIcons={choices.map(s => <MemoryStoreAvatar key={s.name} summary={s} />)} value={source} onChange={value => { setSource(value); setSelection([]); setAllMatching(false); setCounts(null); copy.reset() }} disabled={copy.isPending} />
      <label className="space-y-1 text-[13px]"><span className="block">{t('memoryV2.search')}</span><Input className="w-full" placeholder={t('memoryV2.search')} value={filter} disabled={copy.isPending} onChange={e => setFilter(e.target.value)} /></label>
      <div role="group" aria-label={t('memoryV2.group_kind')} className="flex flex-wrap gap-2">
        {KIND_FILTERS.map(([kind, label]) => {
          const on = kinds.has(kind)
          return <button key={kind} type="button" aria-pressed={on} disabled={copy.isPending || (on && kinds.size === 1)} onClick={() => toggleKind(kind)} className="flex min-h-11 items-center disabled:cursor-default sm:min-h-8">
            <span className={`flex h-8 items-center rounded-full border px-3 text-[13px] ${on ? PILL_ON : 'border-border text-text'}`}>{t(label)}</span>
          </button>
        })}
      </div>
      {/* No hand-off: the selection is an unsaved copy request. */}
      <ErrorNotice message={errorText(data.error || copy.error) || copyFailure} />
      {data.pending && <p role="status" className="py-4 text-center text-[13px] text-muted">{t('pages.overview.memoryTab.loading')}</p>}
      {visible.length > 0 && <label className="flex min-h-11 cursor-pointer items-center gap-3 text-[13px]">
        <input type="checkbox" aria-label={t(searching ? 'memoryV2.copy_select_all' : 'memoryV2.copy_all')} className="h-4 w-4 shrink-0 accent-accent" checked={allMatching} disabled={copy.isPending} onChange={() => { setAllMatching(on => !on); setSelection([]) }} />
        {t(searching ? 'memoryV2.copy_select_all' : 'memoryV2.copy_all')}
      </label>}
      <div className={`space-y-2 ${phone ? '' : 'max-h-80 overflow-y-auto'}`}>
        {visible.map(row => <label key={`${kindOf(row)}:${keyOf(row)}`} className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-[13px] transition-colors hover:bg-bg-hover has-[:checked]:border-accent has-[:checked]:bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]">
          <input type="checkbox" aria-label={bodyOf(row)} className="mt-1 h-4 w-4 shrink-0 accent-accent" checked={picked(row)} disabled={copy.isPending || allMatching} onChange={() => toggle(row)} />
          <span className="min-w-0 break-words"><span className="block text-muted">{t(MEMORY_RECORD_LABELS[kindOf(row)])}</span>{bodyOf(row)}</span>
        </label>)}
      </div>
      {!data.pending && !data.error && data.more && <Btn className="aria-disabled:opacity-30 aria-disabled:cursor-default" aria-disabled={data.loadingMore || undefined} onClick={() => { if (!data.loadingMore) void data.loadMore() }}>{t('memoryV2.show_more')}</Btn>}
      {!data.pending && !data.error && !visible.length && <p className="text-[13px] text-muted">{t('memoryV2.empty')}</p>}
      {copy.data && <div className="text-[13px]">
        {copy.data.results.filter(r => r.reason && !['unconfirmed', 'rejected', 'not_attempted'].includes(r.outcome)).map((r, i) => <p key={`${r.id}:${i}`}>{r.reason}</p>)}
      </div>}
    </div>
  </Modal>
}

/** The Global memory page on its Failures tab (the weekly bell note's link). */
const GLOBAL_FAILURES_ROUTE = memoryRoute({ focus: 'failures' })

export default function MemberMemoryPanel({ store, summary, onDirtyChange, focusLessonKey, focusFailures = false, focusGraph = false, switcher, notices }: {
  store: string; summary: MemoryStoreSummary; onDirtyChange?: (dirty: boolean) => void; focusLessonKey?: string
  /** `?focus=failures`: open on the Failures tab. */
  focusFailures?: boolean
  /** `?focus=graph`: open on the Graph tab. */
  focusGraph?: boolean
  /** The header's store switcher and the store notices under it. */
  switcher?: ReactNode
  notices?: ReactNode
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const privateMemory = summary.memory_version === 2 && !!summary.owner_member
  const legacyMemory = summary.is_default || (
    !summary.owner_member
    && (summary.memory_version === 1 || (summary.memory_version == null && summary.lineage === 'v1'))
  )
  const [copying, setCopying] = useState(false)
  const [recordsDirty, setRecordsDirty] = useState(false)
  const [dirtyDocs, setDirtyDocs] = useState<Record<string, boolean>>({})
  const dirty = recordsDirty || copying || Object.values(dirtyDocs).some(Boolean)
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['memory-records', store] })
    void client.invalidateQueries({ queryKey: ['member-memory', store] })
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
  }
  const total = summary.semantic_count != null && summary.episodic_count != null ? summary.semantic_count + summary.episodic_count : undefined
  const openMember = () => {
    if (!summary.owner_member) return
    const destination = `/members?member=${encodeURIComponent(summary.owner_member)}`
    leave(() => navigate(destination), destination)
  }
  if (!privateMemory && !legacyMemory) {
    return <div className="min-w-0 space-y-4">{switcher}{notices}<Card className="mb-0 min-w-0 py-4">
      <CardTitle>{t('memoryV2.identity_unavailable')}</CardTitle>
      <p className="mt-2 text-[13px] text-muted">{t('pages.kiroCrewAgentsPage.memory_binding_unavailable')}</p>
      <Btn className="mt-3 min-h-11" onClick={() => {
        const destination = summary.owner_member
          ? `/capabilities?tab=crews&crew=${encodeURIComponent(summary.owner_member)}`
          : '/capabilities?tab=crews'
        leave(() => navigate(destination), destination)
      }}>{t('pages.kiroCrewAgentsPage.open_crew_manager')}</Btn>
    </Card></div>
  }
  return <div className="min-w-0 space-y-4">
    <MemoryPageShell id={store} label={summary.owner_member || summary.name} botName={summary.owner_member || undefined} switcher={switcher} embedded={<MemoryEmbeddedLine store={store} />}
      notices={<>{notices}{!privateMemory && !summary.is_default && <p className="text-[13px] text-muted">{t('pages.kiroCrewAgentsPage.private_memory_legacy')}</p>}</>}
      initialTab={focusFailures ? 'failures' : focusGraph ? 'graph' : 'memories'} canCreate={false} canRecall={privateMemory}
      onCopy={privateMemory ? () => setCopying(true) : undefined} copyDisabled={!summary.exists}
      memories={controls => <MemoryList jump={controls.jump} store={store} storeName={summary.owner_member || summary.name} botName={summary.owner_member || undefined} privateMemory={privateMemory} focusKey={focusLessonKey}
        selecting={controls.selecting} onSelectingChange={controls.setSelecting} recallOpen={controls.recallOpen} onRecallOpenChange={controls.setRecallOpen}
        onDirtyChange={setRecordsDirty} onRecovery={() => controls.openTab('recovery')}
        emptyActions={<>{privateMemory && <Btn className="min-h-11" onClick={openMember}><ArrowUpRight className="lucide-inline" />{t('memoryV2.open_member')}</Btn>}{privateMemory && <Btn className="min-h-11" disabled={!summary.exists} onClick={() => setCopying(true)}><Copy className="lucide-inline" />{t('memoryPage.copy_from')}</Btn>}</>} />}
      panels={{
        graph: <MemoryGraphPanel store={store} />,
        profile: <><p className="text-[13px] text-muted">{t('memoryV2.profile_hint')}</p><MemoryDocCard docKey="preferences" store={store} title={t('pages.overview.memoryTab.preferences')} rows={7} placeholder={t('pages.overview.memoryTab.loading')} read={api.memoryPreferences} write={api.saveMemoryPreferences} onDirtyChange={value => setDirtyDocs(old => old.preferences === value ? old : { ...old, preferences: value })} /><MemoryDocCard docKey="projects" store={store} title={t('pages.overview.memoryTab.projects')} rows={7} placeholder={t('pages.overview.memoryTab.loading')} read={api.memoryProjects} write={api.saveMemoryProjects} onDirtyChange={value => setDirtyDocs(old => old.projects === value ? old : { ...old, projects: value })} /></>,
        recovery: <><p className="text-[13px] text-muted">{t('memoryV2.recovery_hint')}</p><MemoryBackupsCard store={store} privateMemory={privateMemory} /><MemoryRetiredCard store={store} privateMemory={privateMemory} /></>,
        // Repeated failures are counted across every chat and their lessons save to Global
        // memory, so a bot page points there instead of saving a lesson outside this store.
        failures: <Card className="mb-0 min-w-0"><p className="text-[13px] text-muted">{t('memoryPage.failures_in_global')}</p><Btn className="mt-3 min-h-11" onClick={() => leave(() => navigate(GLOBAL_FAILURES_ROUTE), GLOBAL_FAILURES_ROUTE)}><ArrowUpRight className="lucide-inline" />{t('memoryPage.open_global_failures')}</Btn></Card>,
        settings: <><Card className="mb-0 min-w-0"><PanelSectionHeader label={t('memoryPage.store_title')} />
          {/* The memory version and saved count the old page header showed. */}
          <ul className="m-0 mt-3 flex list-none flex-wrap items-center gap-x-4 gap-y-1 p-0 text-[13px] text-text"><li>{privateMemory ? t('memoryV2.private_status') : summary.is_default ? t('pages.kiroCrewAgentsPage.global_memory_v1') : t('pages.kiroCrewAgentsPage.legacy_memory_v1')}</li>{total !== undefined && <li>{t('memoryV2.remembered_count', { count: fmtNumber(total) })}</li>}</ul>
          <div className="mt-2"><MemoryStoreStats selected={summary} /></div></Card></>,
      }} />
    {copying && <SeedMemory store={store} member={summary.owner_member || summary.name} onClose={() => setCopying(false)} onCopied={refresh} />}
  </div>
}
