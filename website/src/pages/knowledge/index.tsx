import { useState, useEffect, useRef, useCallback, useMemo, lazy, Suspense } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Search, BookOpen, Network, FolderSync, HelpCircle, FileText, Tag, X, Copy, ChevronDown, ChevronRight, FolderOpen } from 'lucide-react'
import { Btn, SearchInput, Badge, EmptyState, ContentSkeleton } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { knowledgeApi } from './api'
import { typeBadgeVariant, formatDate, useCopy, ITEM_TYPES, STATUSES, ONBOARDING } from './helpers'
import DetailView from './DetailView'
import SourcesList from './SourcesList'
import { EmbeddingStatus } from './EmbeddingStatus'
import type { KnowledgeItem, Entity, Source, NamespaceInfo, IngestionJob } from './types'

const KnowledgeGraph = lazy(() => import('./KnowledgeGraph'))

const TABS = ['list', 'graph', 'sources'] as const
type Tab = typeof TABS[number]

// Backend list_items() hard-caps page size at 100 (dashboard/handlers/knowledge.py).
// The unfiltered list must request exactly that so totalPages math and Prev/Next stay correct.
const MAX_PAGE_SIZE = 100
const TAB_META: Record<Tab, { label: string; icon: React.ReactNode }> = {
  list: { label: 'List View', icon: <FileText size={14} /> },
  graph: { label: 'Graph View', icon: <Network size={14} /> },
  sources: { label: 'Sources', icon: <FolderSync size={14} /> },
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
          {e.mention_count && <span className="text-muted text-[10px] ml-auto">{e.mention_count} mentions</span>}
        </button>
      ))}
    </div>
  )
}

function ItemCard({ item, onClick, selected, onSelect }: { item: KnowledgeItem; onClick: () => void; selected: boolean; onSelect: (checked: boolean) => void }) {
  const { copied, copy } = useCopy()
  return (
    <div className="flex items-start gap-2 animate-rise">
      <input type="checkbox" aria-label={`Select ${item.title || 'Untitled'}`} checked={selected} onChange={e => onSelect(e.target.checked)}
        className="mt-3.5 shrink-0 accent-accent" onClick={e => e.stopPropagation()} />
      <Clickable onClick={onClick} className="flex-1 border border-border rounded-lg p-3.5 hover:border-border-strong hover:bg-bg-hover cursor-pointer transition-all">
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-text-strong truncate">{item.title || 'Untitled'}</div>
            {item.summary && <div className="text-[13px] text-muted mt-1 line-clamp-2">{item.summary}</div>}
          </div>
          <Badge variant={typeBadgeVariant(item.item_type)}>{item.item_type.replace(/_/g, ' ')}</Badge>
        </div>
        <div className="flex items-center gap-3 mt-2 text-[11px] text-muted">
          <span>{formatDate(item.updated_at)}</span>
          {item.namespace && item.namespace !== 'default' && <span className="bg-accent/10 text-accent px-1.5 py-0.5 rounded text-[10px]">{item.namespace}</span>}
          {item.tags && <span className="flex items-center gap-0.5"><Tag size={10} />{typeof item.tags === 'string' ? item.tags : ''}</span>}
          {item._score !== undefined && <span className="text-[10px] text-accent/70">{item._match_type}</span>}
          <Btn className="ml-auto !px-1.5 !py-0.5 !text-[11px]" onClick={e => { e.stopPropagation(); copy(item.summary || item.title) }}><Copy size={10} /> {copied ? 'Copied!' : 'Copy'}</Btn>
        </div>
      </Clickable>
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
      queryClient.setQueriesData<{ items: KnowledgeItem[]; total: number }>({ queryKey: ['knowledge-items'] }, old =>
        old ? { ...old, items: old.items.filter(i => !selectedIds.has(i.id)), total: old.total - selectedIds.size } : old
      )
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
      queryClient.setQueriesData<{ items: KnowledgeItem[]; total: number }>({ queryKey: ['knowledge-items'] }, old =>
        old ? { ...old, items: old.items.filter(i => !selectedIds.has(i.id)), total: old.total - selectedIds.size } : old
      )
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
      <span className="text-[13px] text-text-strong font-medium">{selectedIds.size} selected</span>
      <Btn disabled={pending} onClick={() => bulkArchiveMutation.mutate('archived')}>Archive</Btn>
      <Btn disabled={pending} onClick={() => { if (confirm(`Delete ${selectedIds.size} items permanently?`)) bulkDeleteMutation.mutate() }}>Delete</Btn>
      <Btn onClick={copySelected}><Copy size={12} /> {copied ? 'Copied!' : 'Copy Content'}</Btn>
      <button onClick={onDone} className="ml-auto text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer">Clear</button>
    </div>
  )
}

function FileSubGroup({ filePath, items, onItemClick, selectedItems, onSelect }: {
  filePath: string; items: KnowledgeItem[]
  onItemClick: (id: string) => void; selectedItems: Set<string>; onSelect: (id: string, checked: boolean) => void
}) {
  const [open, setOpen] = useState(true)
  const fileName = filePath === '__ungrouped__' ? 'Other' : filePath.split('/').pop() || filePath

  return (
    <div className="ml-2 border-l-2 border-border pl-2">
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 py-1 text-left bg-transparent border-none cursor-pointer w-full">
        {open ? <ChevronDown size={12} className="text-muted" /> : <ChevronRight size={12} className="text-muted" />}
        <FileText size={12} className="text-muted" />
        <span className="text-[12px] text-text truncate">{fileName}</span>
        <span className="text-[10px] text-muted">({items.length})</span>
      </button>
      {open && (
        <div className="space-y-1 mt-0.5">
          {items.map(item => (
            <ItemCard key={item.id} item={item} onClick={() => onItemClick(item.id)}
              selected={selectedItems.has(item.id)}
              onSelect={(checked) => onSelect(item.id, checked)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

interface SourceGroupProps {
  source: Source | undefined
  items: KnowledgeItem[]
  onItemClick: (id: string) => void
  selectedItems: Set<string>
  onSelect: (id: string, checked: boolean) => void
  defaultOpen?: boolean
  // When true, the badge shows the source's true total (source.item_count) rather than the
  // count of items on the current page, which is capped at MAX_PAGE_SIZE by the backend.
  showSourceTotal?: boolean
}

function SourceGroup({ source, items, onItemClick, selectedItems, onSelect, defaultOpen = false, showSourceTotal = false }: SourceGroupProps) {
  const [open, setOpen] = useState(defaultOpen)
  const name = source?.name || 'Unknown source'
  const subtitle = source?.uri || ''
  const isFolder = source?.source_type === 'local_folder' || source?.source_type === 'obsidian_vault'
  const isArtifact = source?.source_type === 'artifact'
  // Sub-group items: folder/vault sources group by file path; the aggregate
  // "artifact" source groups per-artifact (label = artifact name).
  const isGrouped = isFolder || isArtifact
  const Icon = isFolder ? FolderOpen : FileText

  // Sub-group by file/artifact for grouped sources
  const fileGroups = useMemo(() => {
    if (!isGrouped) return null
    const groups = new Map<string, KnowledgeItem[]>()
    for (const item of items) {
      const key = item._file_path || '__ungrouped__'
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key)!.push(item)
    }
    return groups.size > 0 ? groups : null
  }, [items, isGrouped])

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2.5 bg-bg-elevated hover:bg-bg-hover text-left border-none cursor-pointer transition-colors"
      >
        {open ? <ChevronDown size={14} className="text-muted shrink-0" /> : <ChevronRight size={14} className="text-muted shrink-0" />}
        <Icon size={14} className={isFolder ? "text-amber-500 shrink-0" : "text-accent shrink-0"} />
        <span className="text-[13px] font-medium text-text-strong truncate">{name}</span>
        <Badge variant="ok">{showSourceTotal ? (source?.item_count ?? items.length) : items.length}</Badge>
        {source?.summary_topic && <span className="text-[11px] text-muted truncate max-w-[300px]">{source.summary_topic}</span>}
        {subtitle && <span className="text-[11px] text-muted truncate ml-auto max-w-[200px]">{subtitle}</span>}
      </button>
      {open && (
        <div className="space-y-1.5 p-2 pt-1">
          {fileGroups ? (
            Array.from(fileGroups.entries()).map(([filePath, fileItems]) => (
              <FileSubGroup key={filePath} filePath={filePath} items={fileItems}
                onItemClick={onItemClick} selectedItems={selectedItems} onSelect={onSelect} />
            ))
          ) : (
            items.map(item => (
              <ItemCard key={item.id} item={item} onClick={() => onItemClick(item.id)}
                selected={selectedItems.has(item.id)}
                onSelect={(checked) => onSelect(item.id, checked)}
              />
            ))
          )}
        </div>
      )}
    </div>
  )
}

export default function KnowledgePage() {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<Tab>('list')
  const [query, setQuery] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('active')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedEntity, setSelectedEntity] = useState<string | null>(null)
  const [ingestionJobs, setIngestionJobs] = useState<IngestionJob[]>([])
  const [showHelp, setShowHelp] = useState(false)
  const [namespaceFilter, setNamespaceFilter] = useState('')
  const [uploadNamespace, setUploadNamespace] = useState('default')
  const [showAutocomplete, setShowAutocomplete] = useState(false)
  const [selectedItems, setSelectedItems] = useState<Set<string>>(new Set())
  const searchRef = useRef<HTMLDivElement>(null)
  const entitySectionRef = useRef<HTMLDivElement>(null)
  const listContainerRef = useRef<HTMLDivElement>(null)
  const limit = query ? 20 : MAX_PAGE_SIZE

  const { data: itemsData, isLoading: loading } = useQuery({
    queryKey: ['knowledge-items', { page, query, typeFilter, statusFilter, namespaceFilter, limit }],
    queryFn: () => {
      const params = new URLSearchParams({ page: String(page), limit: String(limit) })
      if (query) params.set('q', query)
      if (typeFilter) params.set('type', typeFilter)
      if (statusFilter) params.set('status', statusFilter)
      if (namespaceFilter) params.set('namespace', namespaceFilter)
      return knowledgeApi<{ items: KnowledgeItem[]; total: number }>(`/items?${params}`)
    },
  })
  // Memoize so the `?? []` fallback doesn't create a new array reference on
  // every render — that reference feeds the groupedItems useMemo and the
  // keyboard-shortcut useEffect below, and an unstable identity would make them
  // recompute/re-subscribe each render.
  const items = useMemo(() => itemsData?.items ?? [], [itemsData])
  const total = itemsData?.total ?? 0
  // Source-group badges show the source's true item_count only when the list is unfiltered.
  // Under a search/type/namespace filter the badge falls back to the matched count on the page,
  // since item_count (from /sources) is the source's unfiltered, all-namespace total.
  const showSourceTotals = !query && !typeFilter && !namespaceFilter

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

  const sourcesMap = useMemo(() => {
    const map = new Map<string, Source>()
    for (const s of sources) map.set(s.id, s)
    return map
  }, [sources])

  const groupedItems = useMemo(() => {
    if (query) return null // flat mode on search
    const groups = new Map<string, KnowledgeItem[]>()
    for (const item of items) {
      const key = item.source_id || '__none__'
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key)!.push(item)
    }
    return groups
  }, [items, query])

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
        if (!selectedId && page < Math.ceil(total / limit)) { setPage(p => p + 1); e.preventDefault() }
      } else if (e.key === 'ArrowLeft' && !e.altKey && !e.ctrlKey) {
        if (!selectedId && page > 1) { setPage(p => p - 1); e.preventDefault() }
      } else if (e.key === 'a' && (e.ctrlKey || e.metaKey) && tab === 'list' && !selectedId) {
        if (listContainerRef.current?.contains(document.activeElement || target)) {
          e.preventDefault()
          setSelectedItems(new Set(items.map(i => i.id)))
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [selectedId, page, total, limit, items, tab, selectedItems.size, showHelp])

  useEffect(() => {
    setSelectedItems(new Set())
  }, [page, query, typeFilter, statusFilter, namespaceFilter])

  useEffect(() => {
    if (selectedEntity && entitySectionRef.current) {
      entitySectionRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [selectedEntity])

  const handleEntitySelect = useCallback((name: string) => {
    setTab('graph')
    setSelectedEntity(name)
    setShowAutocomplete(false)
  }, [])

  const isEmpty = !loading && total === 0 && !query && !typeFilter && !statusFilter
  const totalPages = Math.ceil(total / limit)

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-start sm:items-end justify-between gap-3 sm:gap-4 px-4 sm:px-6 pt-4 pb-3">
        <div className="min-w-0">
          <div className="text-xl sm:text-2xl font-bold tracking-tight text-text-strong flex items-center gap-2">
            <BookOpen size={22} className="shrink-0" /> Knowledge Library
          </div>
          <div className="text-muted text-[13px] sm:text-sm mt-1">Search, explore, and manage your knowledge base</div>
        </div>
        <div className="shrink-0">
          <Btn onClick={() => setShowHelp(true)}><HelpCircle size={14} /> Help</Btn>
        </div>
      </div>

      {showHelp && (
        <Clickable className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50" onClick={e => { if (!e || e.target === e.currentTarget) setShowHelp(false) }}>
          <div role="dialog" aria-modal="true" aria-labelledby="help-title" className="bg-bg-elevated border border-border rounded-xl p-6 max-w-md w-full mx-4 animate-rise">
            <div className="flex items-center justify-between mb-3">
              <h3 id="help-title" className="text-lg font-bold text-text-strong">{ONBOARDING.title}</h3>
              <button aria-label="Close" onClick={() => setShowHelp(false)} className="text-muted hover:text-text bg-transparent border-none cursor-pointer"><X size={18} /></button>
            </div>
            <p className="text-sm text-muted mb-3">{ONBOARDING.description}</p>
            <ol className="space-y-2">
              {ONBOARDING.steps.map((s, i) => <li key={i} className="text-[13px] text-text flex gap-2"><span className="text-accent font-bold">{i + 1}.</span>{s}</li>)}
            </ol>
            <div className="mt-4 pt-3 border-t border-border">
              <div className="text-[12px] font-medium text-text-strong mb-1">Keyboard Shortcuts</div>
              <div className="grid grid-cols-2 gap-1 text-[11px] text-muted">
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">/</kbd> Focus search</span>
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">Esc</kbd> Back / Clear</span>
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">&larr;</kbd> <kbd className="px-1 bg-bg-elevated border border-border rounded">&rarr;</kbd> Prev/Next page</span>
                <span><kbd className="px-1 bg-bg-elevated border border-border rounded">Ctrl+A</kbd> Select all</span>
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
            {TAB_META[t].icon} {TAB_META[t].label}
          </button>
        ))}
      </div>

      <div className={`flex-1 px-4 sm:px-6 py-4 min-h-0 ${tab === 'graph' ? 'flex flex-col' : 'overflow-y-auto'}`} ref={listContainerRef}>
        <EmbeddingStatus />
        {isEmpty && tab === 'list' ? (
          <div className="flex flex-col items-center justify-center py-12 animate-rise">
            <BookOpen size={48} className="text-muted/20 mb-4" />
            <h3 className="text-lg font-bold text-text-strong mb-1">{ONBOARDING.title}</h3>
            <p className="text-sm text-muted mb-4 text-center max-w-md">{ONBOARDING.description}</p>
            <button onClick={() => setTab('sources')} className="px-4 py-2 bg-accent text-accent-fg rounded-md text-sm hover:bg-accent/80 cursor-pointer">Go to Sources to upload files</button>
          </div>
        ) : tab === 'list' ? (
          selectedId ? <DetailView itemId={selectedId} onBack={() => setSelectedId(null)} onEntityClick={handleEntitySelect} /> : (
            <>
              <div className="flex gap-2 mb-3 flex-wrap relative" ref={searchRef}>
                <div className="relative flex-1 min-w-[200px]">
                  <SearchInput placeholder="Search knowledge... (press Enter to search)" value={searchInput}
                    onChange={e => { setSearchInput((e.target as HTMLInputElement).value); setShowAutocomplete(true) }}
                    onKeyDown={e => { if ((e as React.KeyboardEvent).key === 'Enter') { setQuery(searchInput); setPage(1) } }}
                    onFocus={() => setShowAutocomplete(true)}
                    onBlur={() => setTimeout(() => setShowAutocomplete(false), 200)}
                  />
                  {showAutocomplete && searchInput.length >= 2 && (
                    <EntityAutocomplete query={searchInput} onSelect={handleEntitySelect} />
                  )}
                </div>
                <select value={typeFilter} aria-label="Filter by type" onChange={e => { setTypeFilter(e.target.value); setPage(1) }}
                  className="bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-[13px] text-text outline-none">
                  <option value="">All types</option>
                  {ITEM_TYPES.map(t => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
                </select>
                <select value={statusFilter} aria-label="Filter by status" onChange={e => { setStatusFilter(e.target.value); setPage(1) }}
                  className="bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-[13px] text-text outline-none">
                  <option value="">All statuses</option>
                  {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
                <select value={namespaceFilter} aria-label="Filter by namespace" onChange={e => { setNamespaceFilter(e.target.value); setPage(1) }}
                  className="bg-bg-elevated border border-border rounded-md px-2 py-1.5 text-[13px] text-text outline-none">
                  <option value="">All namespaces</option>
                  {namespaces.map(ns => <option key={ns.name} value={ns.name}>{ns.name} ({ns.count})</option>)}
                </select>
              </div>

              {selectedItems.size > 0 && (
                <BulkActions selectedIds={selectedItems} items={items} onDone={() => setSelectedItems(new Set())} />
              )}

              {loading ? <ContentSkeleton /> : !items.length ? (
                <EmptyState icon={<Search size={40} />} title="No items match your search" subtitle="Try different keywords or filters" />
              ) : groupedItems ? (
                <div className="space-y-2 mt-3">
                  {Array.from(groupedItems.entries()).map(([sourceId, groupItems]) => (
                    <SourceGroup
                      key={sourceId}
                      source={sourcesMap.get(sourceId)}
                      items={groupItems}
                      showSourceTotal={showSourceTotals}
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
              {totalPages > 1 && (
                <div className="flex items-center justify-center gap-3 mt-4 py-3 border-t border-border">
                  <Btn disabled={page <= 1} onClick={() => setPage(p => p - 1)}>&larr; Prev</Btn>
                  <span className="text-[13px] text-text font-medium">Page {page} of {totalPages}</span>
                  <span className="text-[11px] text-muted">({total} items)</span>
                  <Btn disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>Next &rarr;</Btn>
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
                  <span className="text-sm font-medium text-text-strong">Items mentioning: <Badge variant="aim">{selectedEntity}</Badge></span>
                  <Btn aria-label="Clear entity selection" onClick={() => { setSelectedEntity(null) }}><X size={12} /></Btn>
                </div>
                {entityItems.length === 0 ? <span className="text-[13px] text-muted">No items found</span> : (
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
          <span className="whitespace-nowrap">{stats.items} items</span>
          <span className="whitespace-nowrap">{stats.entities} entities</span>
          <span className="whitespace-nowrap">{stats.relations} relations</span>
          <span className="whitespace-nowrap">{stats.sources} sources</span>
          {stats.embeddings?.enabled ? (
            <span className={`whitespace-nowrap ${stats.embeddings.available ? 'text-ok' : 'text-warn'}`} title={stats.embeddings.available ? `${stats.embeddings.model} — ${stats.embeddings.embedded_items} embedded` : `Embedding model loading (${stats.embeddings.model})`}>
              ● {stats.embeddings.available ? `embeddings (${stats.embeddings.embedded_items})` : 'embeddings loading'}
            </span>
          ) : (
            <span className="text-muted whitespace-nowrap" title="Embedding model is downloading in the background">○ embeddings initializing</span>
          )}
          {tab === 'list' && <span className="ml-auto text-[10px] hidden sm:inline">/ to search, Esc to back, &larr;&rarr; to page</span>}
        </div>
      )}
    </div>
  )
}
