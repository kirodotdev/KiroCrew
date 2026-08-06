import { useState, useEffect, useRef, useCallback, useMemo, lazy, Suspense } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Search, BookOpen, Network, FolderSync, HelpCircle, FileText, X, Copy } from 'lucide-react'
import { Btn, SearchInput, Badge, EmptyState, ContentSkeleton } from '../../components/ui'
import Clickable from '../../components/Clickable'
import SimpleSelect from '../../components/SimpleSelect'
import { knowledgeApi } from './api'
import { useCopy, ITEM_TYPES, STATUSES, DEFAULT_STATUS_FILTER, ONBOARDING } from './helpers'
import DetailView from './DetailView'
import SourcesList from './SourcesList'
import { ItemCard } from './ItemCard'
import { SourceGroup, NO_SOURCE } from './SourceGroup'
import { EmbeddingStatus } from './EmbeddingStatus'
import type { KnowledgeItem, Entity, Source, NamespaceInfo, IngestionJob } from './types'

/**
 * Visible label per knowledge status. FULL literal keys, resolved at render —
 * not `pages.knowledge.index.status_${s}`: an assembled key is invisible to the
 * key-reference gate, so it cannot be verified to exist and would render its own
 * dotted name to the user if it did not. Module-level `i18nT` is likewise out —
 * it would freeze at the boot language.
 *
 * The status needs catalog copy at all because the closed dropdown trigger
 * DISPLAYS the selected value; the old native `<select>` hid it from view.
 */
const STATUS_LABEL_KEY = {
  active: 'pages.knowledge.index.status_active',
  archived: 'pages.knowledge.index.status_archived',
} as const

import { i18nT } from '../../i18n/t'
const KnowledgeGraph = lazy(() => import('./KnowledgeGraph'))

const TABS = ['list', 'graph', 'sources'] as const
type Tab = typeof TABS[number]

// Backend list_items() hard-caps page size at 100 (dashboard/handlers/knowledge.py).
// The unfiltered list must request exactly that so totalPages math and Prev/Next stay correct.
const MAX_PAGE_SIZE = 100
/**
 * Catalog KEYS for the tab labels, flat and separate from the icons: keys rather
 * than strings because this is evaluated at module load, where an `i18nT()` call
 * would freeze the boot language, and flat because a nested
 * `TAB_META[t].labelKey` is not statically resolvable by
 * `scripts/check-i18n-keys.mjs`. The lookup happens in the tab bar's `.map()`.
 */
const TAB_LABEL_KEY: Record<Tab, string> = {
  list: 'pages.knowledge.index.list_view',
  graph: 'pages.knowledge.index.graph_view',
  // `…index.sources` is NOT reusable here: it already holds the lowercase count
  // noun in "{n} sources" (below), a different string in a different grammatical
  // role. Sharing it would lowercase this tab and let either use-site's
  // translation break the other.
  sources: 'pages.knowledge.index.sources_tab',
}
const TAB_ICON: Record<Tab, React.ReactNode> = {
  list: <FileText size={14} />,
  graph: <Network size={14} />,
  sources: <FolderSync size={14} />,
}

function EntityAutocomplete({ query, onSelect }: { query: string; onSelect: (name: string) => void }) {
  const [debouncedQuery, setDebouncedQuery] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 250)
    return () => clearTimeout(t)
  }, [query])

  const { data: entities = [] } = useQuery({
    queryKey: ['knowledge-entity-autocomplete', debouncedQuery],
    queryFn: () => knowledgeApi<Entity[]>(`/entities?q=${encodeURIComponent(debouncedQuery)}&limit=5`),
    enabled: debouncedQuery.length >= 2,
    staleTime: 500,
    placeholderData: (prev: Entity[] | undefined) => prev,
  })

  if (!entities.length || query.length < 2) return null

  return (
    <div className="absolute top-full left-0 right-0 mt-1 border border-border rounded-md bg-bg-elevated shadow-lg z-20 overflow-hidden">
      {entities.map(e => (
        <button key={e.id} onClick={() => onSelect(e.name)}
          className="w-full px-3 py-2 text-left text-[13px] hover:bg-bg-hover flex items-center gap-2 bg-transparent border-none cursor-pointer">
          <span className="text-accent text-[11px]">{e.entity_type}</span>
          <span className="text-text">{e.name}</span>
          {e.mention_count && <span className="text-muted text-[10px] ml-auto">{e.mention_count} {i18nT('pages.knowledge.index.mentions')}</span>}
        </button>
      ))}
    </div>
  )
}

function BulkActions({ selectedIds, items, onDone }: { selectedIds: Set<string>; items: KnowledgeItem[]; onDone: () => void }) {
  const queryClient = useQueryClient()

  const bulkArchiveMutation = useMutation({
    mutationFn: async (status: string) => {
      await Promise.all(Array.from(selectedIds).map(id =>
        knowledgeApi(`/items/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) })
      ))
    },
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ['knowledge-items'] })
      const prev = queryClient.getQueriesData({ queryKey: ['knowledge-items'] })
      // The 'knowledge-items' prefix also covers the source-counts cache,
      // whose payload has no `items` array. Only rewrite item-shaped entries.
      queryClient.setQueriesData<{ items: KnowledgeItem[]; total: number }>({ queryKey: ['knowledge-items'] }, old => {
        if (!old || !Array.isArray(old.items)) return old
        const kept = old.items.filter(i => !selectedIds.has(i.id))
        return { ...old, items: kept, total: old.total - (old.items.length - kept.length) }
      })
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      ctx?.prev.forEach(([key, data]) => queryClient.setQueryData(key, data))
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
      onDone()
    },
  })

  const bulkDeleteMutation = useMutation({
    mutationFn: async () => {
      await Promise.all(Array.from(selectedIds).map(id =>
        knowledgeApi(`/items/${id}`, { method: 'DELETE' })
      ))
    },
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ['knowledge-items'] })
      const prev = queryClient.getQueriesData({ queryKey: ['knowledge-items'] })
      // The 'knowledge-items' prefix also covers the source-counts cache,
      // whose payload has no `items` array. Only rewrite item-shaped entries.
      queryClient.setQueriesData<{ items: KnowledgeItem[]; total: number }>({ queryKey: ['knowledge-items'] }, old => {
        if (!old || !Array.isArray(old.items)) return old
        const kept = old.items.filter(i => !selectedIds.has(i.id))
        return { ...old, items: kept, total: old.total - (old.items.length - kept.length) }
      })
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      ctx?.prev.forEach(([key, data]) => queryClient.setQueryData(key, data))
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
      onDone()
    },
  })

  const { copied, copy } = useCopy()
  const copySelected = () => {
    const selectedItems = items.filter(i => selectedIds.has(i.id))
    const text = selectedItems.map(i => i.content || i.summary || i.title).join('\n\n---\n\n')
    copy(text)
  }

  const pending = bulkArchiveMutation.isPending || bulkDeleteMutation.isPending

  return (
    <div className="flex items-center gap-2 px-3 py-2 bg-accent/5 border border-accent/20 rounded-lg mb-3">
      <span className="text-[13px] text-text-strong font-medium">{selectedIds.size} {i18nT('pages.knowledge.index.selected')}</span>
      <Btn disabled={pending} onClick={() => bulkArchiveMutation.mutate('archived')}>{i18nT('pages.knowledge.index.archive')}</Btn>
      <Btn disabled={pending} onClick={() => { if (confirm(`Delete ${selectedIds.size} items permanently?`)) bulkDeleteMutation.mutate() }}>{i18nT('pages.knowledge.index.delete')}</Btn>
      <Btn onClick={copySelected}><Copy size={12} /> {copied ? i18nT('pages.knowledge.index.copied') : i18nT('pages.knowledge.index.copy_content')}</Btn>
      <button onClick={onDone} className="ml-auto text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer">{i18nT('pages.knowledge.index.clear')}</button>
    </div>
  )
}

export default function KnowledgePage() {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('list')
  const [query, setQuery] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState(DEFAULT_STATUS_FILTER)
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedEntity, setSelectedEntity] = useState<string | null>(null)
  const [ingestionJobs, setIngestionJobs] = useState<IngestionJob[]>([])
  const [showHelp, setShowHelp] = useState(false)
  const [namespaceFilter, setNamespaceFilter] = useState('')
  const [uploadNamespace, setUploadNamespace] = useState('default')
  const [showAutocomplete, setShowAutocomplete] = useState(false)
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set())
  // Items currently rendered by each expanded SourceGroup. Selection-wide
  // actions read this instead of the react-query cache, so they can never reach
  // an item that is not on screen (a retained cache from a previously expanded
  // source would otherwise be swept into a bulk Delete).
  const [visibleBySource, setVisibleBySource] = useState<Record<string, KnowledgeItem[]>>({})
  const searchRef = useRef<HTMLDivElement>(null)
  const entitySectionRef = useRef<HTMLDivElement>(null)
  const listContainerRef = useRef<HTMLDivElement>(null)
  const limit = query ? 20 : MAX_PAGE_SIZE
  // Source-first list: when nothing is searched the top level renders one row
  // per source and each row pages within itself, so the item-level query below
  // is only used for flat search results.
  const sourceFirst = !query

  const { data: itemsData, isLoading: itemsLoading } = useQuery({
    queryKey: ['knowledge-items', { page, query, typeFilter, statusFilter, namespaceFilter, limit }],
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page), limit: String(limit) })
      if (query) params.set('q', query)
      if (typeFilter) params.set('type', typeFilter)
      if (statusFilter) params.set('status', statusFilter)
      if (namespaceFilter) params.set('namespace', namespaceFilter)
      return knowledgeApi<{ items: KnowledgeItem[]; total: number }>(`/items?${params}`)
    },
    enabled: !sourceFirst,
  })

  // Per-source counts under the active filters. This is what makes every source
  // visible at once: the rows come from these counts, not from whichever items
  // happened to land on a shared page.
  const { data: countsData, isLoading: countsLoading } = useQuery({
    // Shared 'knowledge-items' prefix: see SourceGroup for the rationale.
    queryKey: ['knowledge-items', 'source-counts', { typeFilter, statusFilter, namespaceFilter }],
    queryFn: () => {
      const params = new URLSearchParams()
      if (typeFilter) params.set('type', typeFilter)
      if (statusFilter) params.set('status', statusFilter)
      if (namespaceFilter) params.set('namespace', namespaceFilter)
      const qs = params.toString()
      return knowledgeApi<{ counts: Record<string, number>; total: number }>(`/source-counts${qs ? `?${qs}` : ''}`)
    },
    enabled: sourceFirst,
  })

  // Memoize so the `?? []` fallback doesn't create a new array reference on
  // every render — that reference feeds the keyboard-shortcut useEffect below,
  // and an unstable identity would make it re-subscribe each render.
  const items = useMemo(() => itemsData?.items ?? [], [itemsData])
  const total = sourceFirst ? (countsData?.total ?? 0) : (itemsData?.total ?? 0)
  const loading = sourceFirst ? countsLoading : itemsLoading

  const { data: stats } = useQuery({
    queryKey: ['knowledge-stats'],
    queryFn: () => knowledgeApi<{ items: number; entities: number; relations: number; sources: number; embeddings?: { enabled: boolean; provider?: string; model?: string; available?: boolean; embedded_items?: number } }>('/stats'),
  })

  const { data: namespaces = [] } = useQuery({
    queryKey: ['knowledge-namespaces'],
    queryFn: () => knowledgeApi<NamespaceInfo[]>('/namespaces'),
  })

  const { data: config } = useQuery({
    queryKey: ['knowledge-config'],
    queryFn: () => knowledgeApi<{ enabled: boolean; supported_formats: string[]; accepts_no_extension?: boolean }>('/config'),
  })
  // Build the upload accept filter from the backend's advertised formats
  // (single source of truth) so it never drifts from FileReader.SUPPORTED.
  // Falls back to a superset that includes .pdf if config hasn't loaded yet.
  const uploadAccept = (config?.supported_formats && config.supported_formats.length
    ? config.supported_formats
    : ['.md', '.txt', '.py', '.java', '.ts', '.js', '.rs', '.go', '.html', '.htm',
       '.csv', '.log', '.json', '.yaml', '.yml', '.sh', '.rb', '.c', '.cpp', '.h', '.docx', '.pdf']
  ).filter(Boolean).join(',')
  const acceptsNoExtension = config?.accepts_no_extension ?? true

  const { data: sources = [] } = useQuery({
    queryKey: ['knowledge-sources'],
    queryFn: () => knowledgeApi<Source[]>('/sources'),
    refetchInterval: (query) => {
      const data = query.state.data
      if (data?.some(s => s.sync_status === 'syncing' || s.sync_status === 'active')) return 5000
      return false
    },
  })

  // Invalidate items when any source finishes scanning
  const wasSyncingRef = useRef(false)
  useEffect(() => {
    const isSyncing = sources.some(s => s.sync_status === 'syncing' || s.sync_status === 'active')
    if (wasSyncingRef.current && !isSyncing) {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
    }
    wasSyncingRef.current = isSyncing
  }, [sources, queryClient])

  const { data: entityItems = [] } = useQuery({
    queryKey: ['knowledge-entity-items', selectedEntity],
    queryFn: () => knowledgeApi<KnowledgeItem[]>(`/entities/by-name/${encodeURIComponent(selectedEntity!)}/items`),
    enabled: !!selectedEntity,
  })

  // One row per source that has at least one matching item, ordered the way the
  // Sources tab orders them (most recently updated first) with the sourceless
  // bucket last. Derived from /source-counts, so a source is never hidden just
  // because its items fell on a later page.
  const sourceRows = useMemo(() => {
    if (!sourceFirst) return null
    const counts = countsData?.counts ?? {}
    // Explicit element type: rows for unknown or sourceless buckets carry no
    // Source record, which inference from the first map alone would reject.
    const rows: { sourceId: string; source: Source | undefined; count: number }[] = sources
      .filter(s => (counts[s.id] ?? 0) > 0)
      .map(s => ({ sourceId: s.id, source: s as Source | undefined, count: counts[s.id] }))
    // Sources present in counts but missing from /sources (deleted row, stale
    // cache) still get a row so their items remain reachable.
    const known = new Set(sources.map(s => s.id))
    for (const [sid, count] of Object.entries(counts)) {
      if (sid !== NO_SOURCE && !known.has(sid)) rows.push({ sourceId: sid, source: undefined, count })
    }
    if (counts[NO_SOURCE]) rows.push({ sourceId: NO_SOURCE, source: undefined, count: counts[NO_SOURCE] })
    return rows
  }, [sourceFirst, countsData, sources])

  // Filters forwarded to each group's scoped item fetch. Memoized because it is
  // part of the group query key.
  const groupFilters = useMemo(
    () => ({ type: typeFilter, status: statusFilter, namespace: namespaceFilter }),
    [typeFilter, statusFilter, namespaceFilter]
  )

  const ingestMutation = useMutation({
    mutationFn: async ({ files, namespace }: { files: File[]; namespace: string }) => {
      const jobs = files.map(f => ({ name: f.name, status: 'uploading' }))
      setIngestionJobs(jobs)
      for (let i = 0; i < files.length; i++) {
        const fd = new FormData()
        fd.append('file', files[i])
        try {
          await knowledgeApi<{ job_id: string }>(`/ingest?namespace=${encodeURIComponent(namespace)}`, { method: 'POST', body: fd })
          jobs[i].status = 'done'
        } catch (e: unknown) { jobs[i].status = `error: ${e instanceof Error ? e.message : 'unknown'}` }
        setIngestionJobs([...jobs])
        // Refresh sources/items after each file so they appear immediately
        queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
        queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      }
      return jobs
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-namespaces'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
      setTimeout(() => setIngestionJobs([]), 5000)
    },
  })

  const handleFiles = (files: File[]) => {
    ingestMutation.mutate({ files, namespace: uploadNamespace || 'default' })
  }

  // In flat search mode the page owns the items; in source-first mode the
  // expanded groups do.
  const visibleItems = useMemo(
    () => (sourceFirst ? Object.values(visibleBySource).flat() : items),
    [sourceFirst, visibleBySource, items]
  )

  // Selection is bounded to what is on screen. A group collapsing or paging
  // away removes its items from `visibleItems`, so drop their IDs here too:
  // otherwise a bulk Delete would act on items the user can no longer see.
  useEffect(() => {
    setSelectedItems(prev => {
      if (prev.size === 0) return prev
      const visible = new Set(visibleItems.map(i => i.id))
      const kept = new Set([...prev].filter(id => visible.has(id)))
      return kept.size === prev.size ? prev : kept
    })
  }, [visibleItems])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) {
        if (e.key === 'Escape') {
          (target as HTMLInputElement).blur()
          e.preventDefault()
        }
        return
      }

      if (e.key === '/') {
        e.preventDefault()
        const input = searchRef.current?.querySelector('input')
        input?.focus()
      } else if (e.key === 'Escape') {
        if (showHelp) { setShowHelp(false); e.preventDefault() }
        else if (selectedId) { setSelectedId(null); e.preventDefault() }
        else if (selectedItems.size > 0) { setSelectedItems(new Set()); e.preventDefault() }
      } else if (e.key === 'ArrowRight' && !e.altKey && !e.ctrlKey) {
        // Arrow paging drives the flat search pager only; in source-first mode
        // each group owns its own pager and `page` is unread.
        if (!sourceFirst && !selectedId && page < Math.ceil(total / limit)) { setPage(p => p + 1); e.preventDefault() }
      } else if (e.key === 'ArrowLeft' && !e.altKey && !e.ctrlKey) {
        if (!sourceFirst && !selectedId && page > 1) { setPage(p => p - 1); e.preventDefault() }
      } else if (e.key === 'a' && (e.ctrlKey || e.metaKey) && tab === 'list' && !selectedId) {
        if (listContainerRef.current?.contains(document.activeElement || target)) {
          e.preventDefault()
          // Only what is on screen: never a retained cache for a collapsed source.
          setSelectedItems(new Set(visibleItems.map(i => i.id)))
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [selectedId, page, total, limit, tab, selectedItems.size, showHelp, sourceFirst, visibleItems])

  useEffect(() => {
    setSelectedItems(new Set())
  }, [page, query, typeFilter, statusFilter, namespaceFilter])

  useEffect(() => {
    if (selectedEntity && entitySectionRef.current) {
      entitySectionRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [selectedEntity])

  const handleVisibleItemsChange = useCallback((sourceId: string, groupItems: KnowledgeItem[] | null) => {
    setVisibleBySource(prev => {
      if (groupItems === null) {
        if (!(sourceId in prev)) return prev
        const next = { ...prev }
        delete next[sourceId]
        return next
      }
      if (prev[sourceId] === groupItems) return prev
      return { ...prev, [sourceId]: groupItems }
    })
  }, [])

  const handleEntitySelect = useCallback((name: string) => {
    setTab('graph')
    setSelectedEntity(name)
    setShowAutocomplete(false)
  }, [])

  // "The user has not narrowed anything" — every filter is still at its initial
  // value. statusFilter starts at DEFAULT_STATUS_FILTER, so it must be compared
  // against that default rather than tested for falsiness.
  //
  // namespaceFilter is part of this test because selecting a namespace that
  // holds 0 items would otherwise render onboarding, and that branch replaces
  // the filter bar, leaving no control to clear the filter with.
  //
  // Known gap: a library whose every item is archived also reports 0 active
  // items, and both the list query and /namespaces are active-scoped, so it is
  // indistinguishable from an empty library and shows onboarding. The Sources
  // and Graph tabs stay mounted above this branch, so it is not a dead end.
  const filtersPristine = !query && !typeFilter && !namespaceFilter && statusFilter === DEFAULT_STATUS_FILTER
  const isEmpty = !loading && total === 0 && filtersPristine
  const totalPages = Math.ceil(total / limit)

  // Extracted so the onboarding branch can render it too. Onboarding fires when
  // total === 0 under pristine filters, which includes a library whose every
  // item is archived. Replacing the filter bar in that state would strand the
  // user: statusFilter defaults to active, so the only way back to their
  // content is to change it. Keeping one definition avoids the bar drifting
  // between the two render sites.
  const listFilterBar = (
    <div className="flex gap-2 mb-3 flex-wrap relative" ref={searchRef}>
      <div className="relative flex-1 min-w-[200px]">
        <SearchInput placeholder={i18nT('pages.knowledge.index.search_knowledge_press_enter_to_search')} value={searchInput}
          onChange={e => { setSearchInput((e.target as HTMLInputElement).value); setShowAutocomplete(true) }}
          onKeyDown={e => { if ((e as React.KeyboardEvent).key === 'Enter') { setQuery(searchInput); setPage(1) } }}
          onFocus={() => setShowAutocomplete(true)}
          onBlur={() => setTimeout(() => setShowAutocomplete(false), 200)}
        />
        {showAutocomplete && searchInput.length >= 2 && (
          <EntityAutocomplete query={searchInput} onSelect={handleEntitySelect} />
        )}
      </div>
      {/* The "all" row of each filter is the empty string, the same value the
          state initialises to for type and namespace. SimpleSelect routes ''
          through an internal sentinel, so it stays a selectable option as long
          as '' is present in `options` — which is why it leads each array and
          takes its visible label from the matching `optionLabels` slot. */}
      <SimpleSelect
        options={['', ...ITEM_TYPES]}
        optionLabels={[i18nT('pages.knowledge.index.all_types'), ...ITEM_TYPES.map(t => t.replace(/_/g, ' '))]}
        value={typeFilter}
        onChange={v => { setTypeFilter(v); setPage(1) }}
        aria-label={i18nT('pages.knowledge.index.filter_by_type')}
      />
      <SimpleSelect
        options={['', ...STATUSES]}
        optionLabels={[i18nT('pages.knowledge.index.all_statuses'), ...STATUSES.map(s => i18nT(STATUS_LABEL_KEY[s as keyof typeof STATUS_LABEL_KEY]))]}        value={statusFilter}
        onChange={v => { setStatusFilter(v); setPage(1) }}
        aria-label={i18nT('pages.knowledge.index.filter_by_status')}
      />
      {/* Floor the TRIGGER: the popup matches its width, and namespace names are
          user data, so a placeholder-sized trigger would clip them. */}
      <SimpleSelect
        style={{ minWidth: 180 }}
        options={['', ...namespaces.map(ns => ns.name)]}
        optionLabels={[i18nT('pages.knowledge.index.all_namespaces'), ...namespaces.map(ns => `${ns.name} (${ns.count})`)]}
        value={namespaceFilter}
        onChange={v => { setNamespaceFilter(v); setPage(1) }}
        aria-label={i18nT('pages.knowledge.index.filter_by_namespace')}
      />
    </div>
  )

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-start sm:items-end justify-between gap-3 sm:gap-4 px-4 sm:px-6 pt-2 pb-3">
        <div className="min-w-0">
          <div className="text-xl sm:text-2xl font-bold tracking-tight text-text-strong flex items-center gap-2">
            <BookOpen size={22} className="shrink-0" /> {i18nT('pages.knowledge.index.knowledge_library')}
          </div>
          <div className="text-muted text-[13px] sm:text-sm mt-1">{i18nT('pages.knowledge.index.search_explore_and_manage_your_knowledge_base')}</div>
        </div>
        <div className="shrink-0">
          <Btn onClick={() => setShowHelp(true)}><HelpCircle size={14} /> {i18nT('pages.knowledge.index.help')}</Btn>
        </div>
      </div>

      {showHelp && (
        <Clickable className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50" onClick={e => { if (!e || e.target === e.currentTarget) setShowHelp(false) }}>
          <div role="dialog" aria-modal="true" aria-labelledby="help-title" className="bg-bg-elevated border border-border rounded-xl p-6 max-w-md w-full mx-4 animate-rise">
            <div className="flex items-center justify-between mb-3">
              <h3 id="help-title" className="text-lg font-bold text-text-strong">{ONBOARDING.title}</h3>
              <button aria-label={i18nT('pages.knowledge.index.close')} onClick={() => setShowHelp(false)} className="text-muted hover:text-text bg-transparent border-none cursor-pointer"><X size={18} /></button>
            </div>
            <p className="text-sm text-muted mb-3">{ONBOARDING.description}</p>
            <ol className="space-y-2">
              {ONBOARDING.steps.map((s, i) => <li key={i} className="text-[13px] text-text flex gap-2"><span className="text-accent font-bold">{i + 1}.</span>{s}</li>)}
            </ol>
            <div className="mt-4 pt-3 border-t border-border">
              <div className="text-[12px] font-medium text-text-strong mb-1">{i18nT('pages.knowledge.index.keyboard_shortcuts')}</div>
              <div className="grid grid-cols-2 gap-1 text-[11px] text-muted">
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">/</kbd> {i18nT('pages.knowledge.index.focus_search')}</span>
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">{i18nT('pages.knowledge.index.esc')}</kbd> {i18nT('pages.knowledge.index.back_clear')}</span>
                {/* Arrow glyphs are keycap symbols, not prose — no translation. */}
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">←</kbd> <kbd className="px-1 bg-bg-elevated border border-border rounded">→</kbd> {i18nT('pages.knowledge.index.prev_next_page')}</span>
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">{i18nT('pages.knowledge.index.ctrl_a')}</kbd> {i18nT('pages.knowledge.index.select_all')}</span>
              </div>
            </div>
          </div>
        </Clickable>
      )}

      {/* Tabs — horizontally scrollable on narrow viewports so the active
          underline never spills past the container. */}
      <div className="flex gap-1 px-4 sm:px-6 border-b border-border overflow-x-auto">
        {TABS.map(t => (
          <button key={t} onClick={() => { setTab(t); setSelectedId(null); setSelectedItems(new Set()) }}
            className={`flex items-center gap-1.5 px-3 py-2 text-[13px] font-medium border-b-2 transition-all bg-transparent cursor-pointer shrink-0 whitespace-nowrap ${tab === t ? 'border-accent text-text font-semibold' : 'border-transparent text-muted hover:text-text'}`}>
            {TAB_ICON[t]} {i18nT(TAB_LABEL_KEY[t])}
          </button>
        ))}
      </div>

      <div className={`flex-1 px-4 sm:px-6 py-4 min-h-0 ${tab === 'graph' ? 'flex flex-col' : 'overflow-y-auto'}`} ref={listContainerRef}>
        <EmbeddingStatus />
        {isEmpty && tab === 'list' ? (
          <>
            {listFilterBar}
            <div className="flex flex-col items-center justify-center py-12 animate-rise" data-testid="knowledge-onboarding">
              <BookOpen size={48} className="text-muted/20 mb-4" />
              <h3 className="text-lg font-bold text-text-strong mb-1">{ONBOARDING.title}</h3>
              <p className="text-sm text-muted mb-4 text-center max-w-md">{ONBOARDING.description}</p>
              <button onClick={() => setTab('sources')} className="px-4 py-2 bg-accent text-accent-fg rounded-md text-sm hover:bg-accent/80 cursor-pointer">{i18nT('pages.knowledge.index.go_to_sources_to_upload_files')}</button>
            </div>
          </>
        ) : tab === 'list' ? (
          selectedId ? <DetailView itemId={selectedId} onBack={() => setSelectedId(null)} onEntityClick={handleEntitySelect} /> : (
            <>
              {listFilterBar}

              {selectedItems.size > 0 && (
                <BulkActions selectedIds={selectedItems} items={visibleItems} onDone={() => setSelectedItems(new Set())} />
              )}

              {loading ? <ContentSkeleton /> : sourceRows ? (
                !sourceRows.length ? (
                  <EmptyState icon={<Search size={40} />} title={i18nT('pages.knowledge.index.no_items_match_your_filters')} subtitle={i18nT('pages.knowledge.index.try_a_different_type_status_or_namespace')} />
                ) : (
                  <div className="space-y-2 mt-3">
                    {sourceRows.map(row => (
                      <SourceGroup
                        key={row.sourceId}
                        sourceId={row.sourceId}
                        source={row.source}
                        count={row.count}
                        filters={groupFilters}
                        onVisibleItemsChange={handleVisibleItemsChange}
                        // A single source is opened by default: there is nothing
                        // to choose between, so an extra click buys nothing.
                        defaultOpen={sourceRows.length === 1}
                        onItemClick={(id) => setSelectedId(id)}
                        selectedItems={selectedItems}
                        onSelect={(id, checked) => {
                          setSelectedItems(prev => {
                            const next = new Set(prev)
                            if (checked) next.add(id)
                            else next.delete(id)
                            return next
                          })
                        }}
                      />
                    ))}
                  </div>
                )
              ) : !items.length ? (
                <EmptyState icon={<Search size={40} />} title={i18nT('pages.knowledge.index.no_items_match_your_search')} subtitle={i18nT('pages.knowledge.index.try_different_keywords_or_filters')} />
              ) : (
                <div className="space-y-2 mt-3">
                  {items.map(item => (
                    <ItemCard key={item.id} item={item} onClick={() => setSelectedId(item.id)}
                      selected={selectedItems.has(item.id)}
                      onSelect={(checked) => {
                        setSelectedItems(prev => {
                          const next = new Set(prev)
                          if (checked) next.add(item.id)
                          else next.delete(item.id)
                          return next
                        })
                      }}
                    />
                  ))}
                </div>
              )}
              {/* Top-level pager exists only for flat search results. In
                  source-first mode each group owns its own pager. */}
              {!sourceFirst && totalPages > 1 && (
                <div className="flex items-center justify-center gap-3 mt-4 py-3 border-t border-border">
                  <Btn disabled={page <= 1} onClick={() => setPage(p => p - 1)}>{i18nT('pages.knowledge.index.prev')}</Btn>
                  <span className="text-[13px] text-text font-medium">{i18nT('pages.knowledge.index.page')} {page} {i18nT('pages.knowledge.index.of')} {totalPages}</span>
                  <span className="text-[11px] text-muted">{i18nT('pages.knowledge.index.items_count', { count: total })}</span>
                  <Btn disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>{i18nT('pages.knowledge.index.next')}</Btn>
                </div>
              )}
            </>
          )
        ) : tab === 'graph' ? (
          <div className="flex flex-col gap-4 flex-1 min-h-0">
            <Suspense fallback={<ContentSkeleton />}>
              <KnowledgeGraph highlightEntity={selectedEntity} onSelectEntity={(name) => {
                setSelectedEntity(name)
              }} />
            </Suspense>
            {selectedEntity && (
              <div ref={entitySectionRef} className="border border-accent/30 rounded-lg p-4 bg-accent/5">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-sm font-medium text-text-strong">{i18nT('pages.knowledge.index.items_mentioning')} <Badge variant="aim">{selectedEntity}</Badge></span>
                  <Btn aria-label={i18nT('pages.knowledge.index.clear_entity_selection')} onClick={() => { setSelectedEntity(null) }}><X size={12} /></Btn>
                </div>
                {entityItems.length === 0 ? <span className="text-[13px] text-muted">{i18nT('pages.knowledge.index.no_items_found')}</span> : (
                  <div className="flex flex-col gap-1">
                    {entityItems.map(it => (
                      <Clickable key={it.id} onClick={() => { setSelectedId(it.id); setTab('list') }}
                        className="text-[13px] text-accent hover:underline">{it.title}</Clickable>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ) : (
          <SourcesList onIngest={handleFiles} uploadNamespace={uploadNamespace} setUploadNamespace={setUploadNamespace} namespaces={namespaces} ingestionJobs={ingestionJobs} uploadAccept={uploadAccept} acceptsNoExtension={acceptsNoExtension} />
        )}
      </div>

      {/* Stats bar */}
      {stats && (
        <div className="border-t border-border px-4 sm:px-6 py-2 flex gap-x-3 gap-y-0.5 sm:gap-4 flex-wrap text-[11px] sm:text-[12px] text-muted shrink-0">
          <span className="whitespace-nowrap">{stats.items} {i18nT('pages.knowledge.index.items_2')}</span>
          <span className="whitespace-nowrap">{stats.entities} {i18nT('pages.knowledge.index.entities')}</span>
          <span className="whitespace-nowrap">{stats.relations} {i18nT('pages.knowledge.index.relations')}</span>
          <span className="whitespace-nowrap">{stats.sources} {i18nT('pages.knowledge.index.sources')}</span>
          {stats.embeddings?.enabled ? (
            <span className={`whitespace-nowrap ${stats.embeddings.available ? 'text-ok' : 'text-warn'}`} title={stats.embeddings.available ? `${stats.embeddings.model} — ${stats.embeddings.embedded_items} embedded` : i18nT('pages.knowledge.index.embedding_model_loading', { name: stats.embeddings.model })}>
              ● {stats.embeddings.available ? `embeddings (${stats.embeddings.embedded_items})` : i18nT('pages.knowledge.index.embeddings_loading')}
            </span>
          ) : (
            <span className="text-muted whitespace-nowrap" title={i18nT('pages.knowledge.index.embedding_model_is_downloading_in_the_background')}>{i18nT('pages.knowledge.index.embeddings_initializing')}</span>
          )}
          {tab === 'list' && <span className="ml-auto text-[10px] hidden sm:inline">{i18nT('pages.knowledge.index.to_search_esc_to_back_to_page')}</span>}
        </div>
      )}
    </div>
  )
}
