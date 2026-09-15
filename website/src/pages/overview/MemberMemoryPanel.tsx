import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { Brain, Copy, LockKeyhole, History, SlidersHorizontal, BookOpen, ArrowUpRight } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Input } from '../../components/ui'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs'
import MemoryRecordsEditor, { MEMORY_RECORD_LABELS } from './MemoryRecordsEditor'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import SimpleSelect from '../../components/SimpleSelect'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { fmtNumber } from '../../i18n/format'
import type { MemoryStoreSummary } from '../../types'
import { semanticValueText } from './VectorMemoryCard'
import { MEMORY_QUERY_PREFIXES, MemoryStoreAvatar, memoryQueryRetry, useMemoryStores } from './MemoryStoreCard'
import MemoryRetiredCard from './MemoryRetiredCard'
import MemoryBackupsCard from './MemoryBackupsCard'
import MemoryCarveCard from './MemoryCarveCard'
import MemoryDocCard from './MemoryDocCard'

type Kind = 'fact' | 'directive' | 'episode'
type MemoryRow = { key?: string; id?: string; value_json?: unknown; text?: string; source?: string; created_at?: string; derived_from?: string }
type Selection = { kind: Kind; id: string }
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
  const data = useMemoryRows(source, true, filter)
  const copy = useMutation({ mutationFn: () => api.memorySeed(source, store, selection), onSuccess: onCopied })
  const failedResults = copy.data?.results.filter(r => ['unconfirmed', 'rejected', 'not_attempted'].includes(r.outcome)) ?? []
  const failureReasons = failedResults.flatMap(result => result.reason?.trim() ? [result.reason.trim()] : [])
  const copyFailure = [
    copy.data?.partial ? t('memoryV2.copy_partial') : '',
    ...failureReasons,
    failureReasons.length < failedResults.length ? t('appSdk.chatMessageList.copy_failed') : '',
  ].filter(Boolean).join('\n')
  const choices = (stores.data?.stores || []).filter(s => s.name !== store && s.exists)
  const visible = data.pending || data.error ? [] : data.rows
  const toggle = (row: MemoryRow) => {
    const item = { kind: kindOf(row), id: keyOf(row) }
    setSelection(old => old.some(s => s.id === item.id && s.kind === item.kind) ? old.filter(s => s.id !== item.id || s.kind !== item.kind) : [...old, item])
  }
  return <Modal open title={t('memoryV2.copy_dialog_title', { member })} guardAccidentalDismiss={selection.length > 0} onClose={copy.isPending ? () => {} : onClose} footer={<div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
    <span className="text-[12px] text-muted">{t('memoryV2.copy_limit')}</span>
    <Btn primary className="min-h-11 justify-center" disabled={copy.isPending || !!data.error || !selection.length || selection.length > 50} onClick={() => copy.mutate()}><Copy className="lucide-inline" />{t('memoryV2.copy_selected', { count: fmtNumber(selection.length) })}</Btn>
  </div>}>
    <div className="flex min-w-0 flex-col gap-3">
      <p className="text-[13px] text-muted">{t('memoryV2.copy_explanation')}</p>
      <SimpleSelect aria-label={t('memoryV2.source')} options={choices.map(s => s.name)} optionLabels={choices.map(s => s.is_default ? t('pages.kiroCrewAgentsPage.global_memory_v1') : s.owner_member || s.name)} optionIcons={choices.map(s => <MemoryStoreAvatar key={s.name} summary={s} />)} value={source} onChange={value => { setSource(value); setSelection([]); copy.reset() }} disabled={copy.isPending} />
      <label className="space-y-1 text-[13px]"><span className="block">{t('memoryV2.search')}</span><Input className="w-full" placeholder={t('memoryV2.search')} value={filter} onChange={e => setFilter(e.target.value)} /></label>
      {/* No hand-off: the selection is an unsaved copy request. */}
      <ErrorNotice message={errorText(data.error || copy.error) || copyFailure} />
      {data.pending && <p role="status" className="py-4 text-center text-[13px] text-muted">{t('pages.overview.memoryTab.loading')}</p>}
      <div className="max-h-80 overflow-y-auto space-y-2">
        {visible.map(row => <label key={`${kindOf(row)}:${keyOf(row)}`} className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-[13px] transition-colors hover:bg-bg-hover has-[:checked]:border-accent has-[:checked]:bg-accent-subtle">
          <input type="checkbox" aria-label={bodyOf(row)} className="mt-1 h-4 w-4 shrink-0 accent-accent" checked={selection.some(s => s.id === keyOf(row) && s.kind === kindOf(row))} disabled={copy.isPending || (selection.length >= 50 && !selection.some(s => s.id === keyOf(row) && s.kind === kindOf(row)))} onChange={() => toggle(row)} />
          <span className="min-w-0 break-words"><span className="block text-muted">{t(MEMORY_RECORD_LABELS[kindOf(row)])}</span>{bodyOf(row)}</span>
        </label>)}
      </div>
      {!data.pending && !data.error && data.more && <Btn disabled={data.loadingMore} onClick={() => void data.loadMore()}>{t('memoryV2.show_more')}</Btn>}
      {!data.pending && !data.error && !visible.length && <p className="text-[13px] text-muted">{t('memoryV2.empty')}</p>}
      {copy.data && <div className="text-[13px]">
        <p>{t('memoryV2.copy_source_unchanged')}</p>
        {!copy.data.partial && <p role="status">{t('memoryV2.copy_result', { imported: fmtNumber(copy.data.results.filter(r => r.outcome === 'imported').length), skipped: fmtNumber(copy.data.results.filter(r => ['existing', 'rejected', 'not_attempted'].includes(r.outcome)).length) })}</p>}
        {copy.data.results.filter(r => r.reason && !['unconfirmed', 'rejected', 'not_attempted'].includes(r.outcome)).map((r, i) => <p key={`${r.id}:${i}`}>{r.reason}</p>)}
      </div>}
    </div>
  </Modal>
}

export default function MemberMemoryPanel({ store, summary, onDirtyChange }: { store: string; summary: MemoryStoreSummary; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const privateMemory = summary.memory_version === 2 && !!summary.owner_member
  const legacyMemory = summary.is_default || (
    !summary.owner_member
    && (summary.memory_version === 1 || (summary.memory_version == null && summary.lineage === 'v1'))
  )
  const [section, setSection] = useState('memories')
  const [visited, setVisited] = useState<Set<string>>(() => new Set(['memories']))
  const [advanced, setAdvanced] = useState(false)
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
  const empty = total === 0
  const copyAction = privateMemory && <Btn primary={empty} className="min-h-11 justify-center" onClick={() => setCopying(true)} disabled={!summary.exists}><Copy className="lucide-inline" />{t('memoryV2.copy_action')}</Btn>
  const selectSection = (next: string) => { setSection(next); setVisited(old => new Set([...old, next])) }
  const openMember = () => {
    if (!summary.owner_member) return
    const destination = `/members?member=${encodeURIComponent(summary.owner_member)}`
    leave(() => navigate(destination), destination)
  }
  if (!privateMemory && !legacyMemory) {
    return <Card className="mb-0 min-w-0 py-4">
      <CardTitle>{t('memoryV2.identity_unavailable')}</CardTitle>
      <p className="mt-2 text-[13px] text-muted">{t('pages.kiroCrewAgentsPage.memory_binding_unavailable')}</p>
      <Btn className="mt-3 min-h-11" onClick={() => {
        const destination = summary.owner_member
          ? `/capabilities?tab=crews&crew=${encodeURIComponent(summary.owner_member)}`
          : '/capabilities?tab=crews'
        leave(() => navigate(destination), destination)
      }}>{t('pages.kiroCrewAgentsPage.open_crew_manager')}</Btn>
    </Card>
  }
  return <div className="min-w-0 space-y-4">
    <Card className="mb-0 min-w-0 py-4">
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <div className="relative shrink-0 rounded-md shadow-[0_0_24px_var(--accent-glow)]"><MemoryStoreAvatar summary={summary} size={48} />{privateMemory && <span className="absolute -bottom-1 -right-1 flex h-5 w-5 items-center justify-center rounded-full border border-border bg-card text-[10px] text-text"><LockKeyhole className="lucide-inline" /></span>}</div>
          <div className="min-w-0 space-y-2"><CardTitle className="mb-0 min-w-0 text-lg leading-snug"><span className="min-w-0 break-words">{t('memoryV2.title', { member: summary.owner_member || summary.name })}</span></CardTitle><ul className="m-0 flex list-none flex-wrap items-center gap-x-3 gap-y-1 p-0 text-[12px] text-muted"><li>{privateMemory ? t('memoryV2.private_status') : summary.is_default ? t('pages.kiroCrewAgentsPage.global_memory_v1') : t('pages.kiroCrewAgentsPage.legacy_memory_v1')}</li>{total !== undefined && <li>{t('memoryV2.remembered_count', { count: fmtNumber(total) })}</li>}</ul></div>
        </div>
        {copyAction && <div className="shrink-0">{copyAction}</div>}
      </div>
    </Card>
    {!privateMemory && !summary.is_default && <p className="text-[13px] text-muted">{t('pages.kiroCrewAgentsPage.private_memory_legacy')}</p>}
    <Tabs value={section} layoutId={`member-memory-sections-${store}`} onValueChange={selectSection}>
      <div className="mb-4 border-b border-border pb-3"><TabsList aria-label={t('memoryV2.title', { member: summary.owner_member || summary.name })} className="w-full sm:w-fit"><TabsTrigger value="memories" className="min-h-11 flex-1 justify-center sm:flex-none"><Brain className="lucide-inline" />{t('memoryV2.tab_memories')}</TabsTrigger><TabsTrigger value="profile" className="min-h-11 flex-1 justify-center sm:flex-none"><BookOpen className="lucide-inline" />{t('memoryV2.tab_profile')}</TabsTrigger><TabsTrigger value="recovery" className="min-h-11 flex-1 justify-center sm:flex-none"><History className="lucide-inline" />{t('memoryV2.tab_recovery')}</TabsTrigger></TabsList></div>
      <TabsContent value="memories" forceMount hidden={section !== 'memories'}><MemoryRecordsEditor store={store} privateMemory={privateMemory} onDirtyChange={setRecordsDirty} onRecovery={() => selectSection('recovery')} emptyActions={<>{privateMemory && <Btn className="min-h-11" onClick={openMember}><ArrowUpRight className="lucide-inline" />{t('memoryV2.open_member')}</Btn>}</>} /></TabsContent>
      <TabsContent value="profile" forceMount hidden={section !== 'profile'} className="space-y-4">{visited.has('profile') && <><p className="text-[13px] text-muted">{t('memoryV2.profile_hint')}</p><MemoryDocCard docKey="preferences" store={store} title={t('pages.overview.memoryTab.preferences')} rows={7} placeholder={t('pages.overview.memoryTab.loading')} read={api.memoryPreferences} write={api.saveMemoryPreferences} onDirtyChange={value => setDirtyDocs(old => old.preferences === value ? old : { ...old, preferences: value })} /><MemoryDocCard docKey="projects" store={store} title={t('pages.overview.memoryTab.projects')} rows={7} placeholder={t('pages.overview.memoryTab.loading')} read={api.memoryProjects} write={api.saveMemoryProjects} onDirtyChange={value => setDirtyDocs(old => old.projects === value ? old : { ...old, projects: value })} /></>}</TabsContent>
      <TabsContent value="recovery" forceMount hidden={section !== 'recovery'} className="space-y-4">{visited.has('recovery') && <><p className="text-[13px] text-muted">{t('memoryV2.recovery_hint')}</p><MemoryBackupsCard store={store} privateMemory={privateMemory} /><MemoryRetiredCard store={store} privateMemory={privateMemory} /><details onToggle={event => setAdvanced(event.currentTarget.open)}><summary className="cursor-pointer rounded-lg border border-border p-3 text-[13px] text-muted"><SlidersHorizontal className="lucide-inline mr-2" />{t('memoryV2.advanced')}</summary>{advanced && <div className="mt-3"><MemoryCarveCard store={store} /></div>}</details></>}</TabsContent>
    </Tabs>
    {copying && <SeedMemory store={store} member={summary.owner_member || summary.name} onClose={() => setCopying(false)} onCopied={refresh} />}
  </div>
}
