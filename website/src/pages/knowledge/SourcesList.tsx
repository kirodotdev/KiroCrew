import { useState, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Upload, FolderSync, FolderOpen, X, RefreshCw, AlertCircle, CheckCircle, ChevronDown, ChevronRight, Pause, Play, Pencil, Check } from 'lucide-react'
import { Badge, EmptyState, ContentSkeleton } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { knowledgeApi } from './api'
import { formatRelativeDate, SUPPORTED_FORMATS } from './helpers'
import { parseSourceProps, shouldShowWordCount } from './knowledgeUtils'
import type { Source, NamespaceInfo, IngestionJob, SourceFilesResponse } from './types'

export function SourceSummaryDisplay({ source }: { source: Source }) {
  if (!source.summary_topic) return null
  const themes: string[] = (() => { try { return JSON.parse(source.summary_themes || '[]') } catch { return [] } })()
  return (
    <div className="mt-1 min-w-0">
      <p className="text-[12px] text-muted leading-relaxed line-clamp-1">{source.summary_topic}</p>
      {themes.length > 0 && (
        <div className="flex gap-1 flex-wrap mt-0.5">
          {themes.map((t, i) => <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-bg border border-border text-muted whitespace-nowrap max-w-full truncate">{t}</span>)}
        </div>
      )}
    </div>
  )
}

function NamespacePicker({ value, onChange, namespaces }: { value: string; onChange: (v: string) => void; namespaces: NamespaceInfo[] }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[12px] text-muted shrink-0">Namespace:</span>
      <input value={value} onChange={e => onChange(e.target.value)} placeholder="default"
        aria-label="Namespace"
        className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none w-36"
        list="ns-picker-list" />
      <datalist id="ns-picker-list">
        {namespaces.map(ns => <option key={ns.name} value={ns.name}>{ns.name} ({ns.count})</option>)}
      </datalist>
      <span className="text-[10px] text-muted">Type new to create</span>
    </div>
  )
}

function DropZone({ onFiles, accept }: { onFiles: (files: File[]) => void; accept?: string }) {
  const [over, setOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <Clickable
      onDragOver={e => { e.preventDefault(); setOver(true) }}
      onDragLeave={() => setOver(false)}
      onDrop={e => { e.preventDefault(); setOver(false); onFiles(Array.from(e.dataTransfer.files)) }}
      onClick={() => inputRef.current?.click()}
      className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-all ${over ? 'border-accent bg-accent-subtle' : 'border-border hover:border-border-strong'}`}
    >
      <Upload size={28} className="mx-auto mb-2 text-muted" />
      <div className="text-sm text-muted">Drop files here or click to upload</div>
      <div className="text-[11px] text-muted/50 mt-1">{SUPPORTED_FORMATS}</div>
      <input ref={inputRef} type="file" multiple accept={accept} aria-label="Upload files" className="hidden" onChange={e => e.target.files && onFiles(Array.from(e.target.files))} />
    </Clickable>
  )
}

function IngestionProgress({ jobs }: { jobs: IngestionJob[] }) {
  if (!jobs.length) return null
  return (
    <div className="space-y-1.5 mt-3">
      {jobs.map((j, i) => (
        <div key={i} className="flex items-center gap-2 text-[13px]">
          {j.status === 'done' ? <CheckCircle size={14} className="text-ok" /> : j.status.startsWith('error') ? <AlertCircle size={14} className="text-danger" /> : <RefreshCw size={14} className="text-accent animate-spin" />}
          <span className="text-text truncate flex-1">{j.name}</span>
          <span className="text-muted text-[11px]">{j.status}</span>
        </div>
      ))}
    </div>
  )
}

function StalenessIndicator({ lastSynced }: { lastSynced?: string }) {
  if (!lastSynced) return <span className="text-[11px] text-muted">never synced</span>
  const daysSince = Math.floor((Date.now() - new Date(lastSynced).getTime()) / (1000 * 60 * 60 * 24))
  const stale = daysSince > 30
  return (
    <span className={`text-[11px] whitespace-nowrap ${stale ? 'text-warn' : 'text-muted'}`}>
      {stale && <AlertCircle size={10} className="inline mr-0.5" />}
      {formatRelativeDate(lastSynced)}
    </span>
  )
}

function FolderConfirmDialog({ fileCount, uri, onConfirm, onCancel, isPending }: {
  fileCount: number; uri: string; onConfirm: () => void; onCancel: () => void; isPending: boolean
}) {
  const isLarge = fileCount > 100
  return (
    <div className="border border-border rounded-lg p-4 bg-bg-elevated space-y-2">
      <div className="text-sm font-medium flex items-center gap-1.5">
        {isLarge && <AlertCircle size={14} className="text-warn" />}
        <FolderOpen size={14} className="inline" /> {uri} — {fileCount === 0 ? 'empty (0 supported files)' : `${fileCount} supported file${fileCount > 1 ? 's' : ''} found`}
      </div>
      {isLarge && (
        <div className="text-[12px] text-warn">
          Scanning this many files will take several minutes and use significant resources.
        </div>
      )}
      <div className="text-[12px] text-muted">
        {fileCount === 0
          ? 'This folder will be watched. Any supported files added here will be auto-ingested on the next scan cycle (~5 min).'
          : 'This folder will be watched continuously. New files added here will be auto-ingested on the next scan cycle (~5 min).'}
      </div>
      <div className="flex gap-2 justify-end pt-1">
        <button onClick={onCancel} className="px-3 py-1.5 text-xs border border-border rounded-md text-text">Cancel</button>
        <button onClick={onConfirm} disabled={isPending}
          className="px-3 py-1.5 text-xs bg-accent text-accent-fg rounded-md disabled:opacity-50">
          {isPending ? 'Starting...' : fileCount === 0 ? 'Watch Anyway' : 'Start Scanning'}
        </button>
      </div>
    </div>
  )
}

function FolderProgress({ sourceId }: { sourceId: string }) {
  const queryClient = useQueryClient()
  const wasScanning = useRef(false)
  const { data } = useQuery({
    queryKey: ['source-files', sourceId],
    queryFn: () => knowledgeApi<SourceFilesResponse>(`/sources/${sourceId}/files`),
    refetchInterval: (query) => {
      const d = query.state.data
      if (d && d.total > 0 && d.done + d.failed + d.skipped >= d.total) return false
      return 3000
    },
  })

  // Invalidate list/graph views when scan completes
  useEffect(() => {
    if (!data || data.total === 0) return
    const complete = data.done + data.failed + data.skipped >= data.total
    if (wasScanning.current && complete) {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-graph'] })
    }
    wasScanning.current = !complete
  }, [data, queryClient])

  const retryMutation = useMutation({
    mutationFn: (filePath: string) => knowledgeApi(`/sources/${sourceId}/files/retry`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file_path: filePath })
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['source-files', sourceId] }),
  })

  const skipMutation = useMutation({
    mutationFn: (filePath: string) => knowledgeApi(`/sources/${sourceId}/files/skip`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file_path: filePath })
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['source-files', sourceId] }),
  })

  if (!data || data.total === 0) return null

  const pct = Math.round((data.done / data.total) * 100)
  const scanning = data.files.find(f => f.status === 'scanning')
  const failed = data.files.filter(f => f.status === 'failed')
  const recentDone = data.files.filter(f => f.status === 'done').slice(0, 3)

  return (
    <div className="border-t border-border mt-2 pt-2 space-y-1.5">
      {/* Progress bar */}
      <div className="flex items-center gap-2 text-[11px] text-muted">
        <div className="flex-1 h-1.5 bg-bg rounded-full overflow-hidden">
          <div className="h-full bg-accent rounded-full transition-all" style={{ width: `${pct}%` }} />
        </div>
        <span>{data.done}/{data.total} ({pct}%)</span>
        {data.failed > 0 && <span className="text-danger">{data.failed} failed</span>}
      </div>
      {/* Currently scanning */}
      {scanning && (
        <div className="flex items-center gap-1.5 text-[12px]">
          <RefreshCw size={11} className="text-accent animate-spin" />
          <span className="text-text truncate">{scanning.file_path.split('/').pop()}</span>
        </div>
      )}
      {/* Recent done */}
      {recentDone.map(f => (
        <div key={f.file_path} className="flex items-center gap-1.5 text-[12px]">
          <CheckCircle size={11} className="text-ok" />
          <span className="text-muted truncate">{f.file_path.split('/').pop()}</span>
          <span className="text-[10px] text-muted ml-auto">{f.item_count} items</span>
        </div>
      ))}
      {/* Failed files */}
      {failed.map(f => (
        <div key={f.file_path} className="flex items-center gap-1.5 text-[12px]">
          <AlertCircle size={11} className="text-danger" />
          <span className="text-text truncate flex-1" title={f.error_message || ''}>{f.file_path.split('/').pop()}</span>
          <span className="text-[10px] text-danger truncate max-w-[120px]">{f.error_message}</span>
          <button onClick={() => retryMutation.mutate(f.file_path)} className="text-[10px] text-accent hover:underline">Retry</button>
          <button onClick={() => skipMutation.mutate(f.file_path)} className="text-[10px] text-muted hover:underline">Skip</button>
        </div>
      ))}
    </div>
  )
}

export default function SourcesList({ onIngest, uploadNamespace, setUploadNamespace, namespaces, ingestionJobs, uploadAccept, acceptsNoExtension }: {
  onIngest: (files: File[]) => void; uploadNamespace: string; setUploadNamespace: (v: string) => void
  namespaces: NamespaceInfo[]; ingestionJobs: IngestionJob[]
  uploadAccept?: string; acceptsNoExtension?: boolean
}) {
  const queryClient = useQueryClient()
  const [showAdd, setShowAdd] = useState(false)
  const [addType, setAddType] = useState<'local_file' | 'local_folder'>('local_file')
  const [addUri, setAddUri] = useState('')
  const [addName, setAddName] = useState('')
  const [addIgnorePatterns, setAddIgnorePatterns] = useState('')
  const [addRecursive, setAddRecursive] = useState(true)
  const [pendingConfirm, setPendingConfirm] = useState<{ id: string; uri: string; fileCount: number } | null>(null)
  const [expandedSource, setExpandedSource] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState('')

  const { data: sources = [], isLoading: loading } = useQuery({
    queryKey: ['knowledge-sources'],
    queryFn: () => knowledgeApi<Source[]>('/sources'),
    // Auto-refresh when any source has a pending sync status
    refetchInterval: (query) => {
      const data = query.state.data
      if (data?.some(s => s.sync_status === 'syncing')) return 5000
      return false
    },
  })

  // Invalidate items/graph when sync completes
  const wasSyncingRef = useRef(false)
  useEffect(() => {
    const isSyncing = sources.some(s => s.sync_status === 'syncing')
    if (wasSyncingRef.current && !isSyncing) {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-graph'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
    }
    wasSyncingRef.current = isSyncing
  }, [sources, queryClient])

  const [syncingIds, setSyncingIds] = useState<Set<string>>(new Set())

  const syncSource = async (id: string) => {
    setSyncingIds(prev => new Set(prev).add(id))
    try {
      await knowledgeApi<{ synced?: boolean; status?: string }>(`/sources/${id}/sync`, { method: 'POST' })
      queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
    } finally {
      setSyncingIds(prev => { const next = new Set(prev); next.delete(id); return next })
    }
  }

  const { data: kbConfig } = useQuery({
    queryKey: ['knowledge-config'],
    queryFn: () => knowledgeApi<{ enabled: boolean; supported_formats: string[]; folder_picker?: boolean }>('/config'),
    staleTime: 60_000,
  })
  const folderPickerAvailable = kbConfig?.folder_picker ?? false

  const pickFolderMutation = useMutation({
    mutationFn: () => knowledgeApi<{ path?: string | null }>('/pick-folder', { method: 'POST' }),
    onSuccess: (res) => { if (res.path) setAddUri(res.path) },
  })

  const addMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      knowledgeApi<{ id: string; status?: string; file_count?: number }>('/sources', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    onSuccess: (res) => {
      if (res.status === 'pending_confirmation') {
        setPendingConfirm({ id: res.id, uri: addUri, fileCount: res.file_count ?? 0 })
        setShowAdd(false); setAddUri(''); setAddName(''); setAddIgnorePatterns('')
        queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
      } else {
        setShowAdd(false); setAddUri(''); setAddName(''); setAddIgnorePatterns('')
        queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
        if (res.id) syncSource(res.id)
      }
    },
  })

  const confirmMutation = useMutation({
    mutationFn: (id: string) => knowledgeApi(`/sources/${id}/confirm`, { method: 'POST' }),
    onSuccess: () => {
      setPendingConfirm(null)
      queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
    },
  })

  const pauseMutation = useMutation({
    mutationFn: (id: string) => knowledgeApi(`/sources/${id}/pause`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] }),
  })

  const resumeMutation = useMutation({
    mutationFn: (id: string) => knowledgeApi(`/sources/${id}/resume`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] }),
  })

  const renameMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      knowledgeApi(`/sources/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }),
    onSuccess: () => {
      setEditingId(null)
      queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
    },
  })

  const startRename = (s: Source) => { setEditingId(s.id); setEditDraft(s.name) }
  const submitRename = () => {
    if (!editingId) return
    const name = editDraft.trim()
    const current = sources.find(s => s.id === editingId)
    if (!name || (current && name === current.name)) { setEditingId(null); return }
    renameMutation.mutate({ id: editingId, name })
  }

  const deleteMutation = useMutation({
    mutationFn: (id: string) => knowledgeApi(`/sources/${id}`, { method: 'DELETE' }),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: ['knowledge-sources'] })
      const prev = queryClient.getQueryData<Source[]>(['knowledge-sources'])
      queryClient.setQueryData<Source[]>(['knowledge-sources'], old => old?.filter(s => s.id !== id))
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      queryClient.setQueryData(['knowledge-sources'], ctx?.prev)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-sources'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-graph'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
    },
  })

  const handleAdd = () => {
    if (!addUri.trim()) return
    const body: Record<string, unknown> = { name: addName || addUri, source_type: addType, uri: addUri }
    if (addType === 'local_folder') {
      const props: Record<string, unknown> = { recursive: addRecursive }
      if (addIgnorePatterns.trim()) props.ignore_patterns = addIgnorePatterns.split('\n').map(s => s.trim()).filter(Boolean)
      if (uploadNamespace && uploadNamespace !== 'default') props.namespace = uploadNamespace
      body.properties = props
    }
    addMutation.mutate(body)
  }

  if (loading) return <ContentSkeleton />

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button onClick={() => setShowAdd(true)} className="px-3 py-1.5 text-[13px] bg-accent text-accent-fg rounded-md hover:bg-accent/80">+ Add Source</button>
      </div>

      {showAdd && (
        <div className="border border-border rounded-lg p-4 bg-bg-elevated space-y-3">
          <div className="text-sm font-medium">Add Source</div>
          <div className="flex gap-2 flex-wrap">
            {(['local_file', 'local_folder'] as const).map(t => (
              <button key={t} onClick={() => setAddType(t)}
                className={`px-3 py-1.5 text-[13px] rounded-md border flex items-center gap-1 ${addType === t ? 'border-accent bg-accent/10 text-accent' : 'border-border text-muted'}`}>
                {t === 'local_file' ? <><Upload size={12} /> Local File</> : <><FolderOpen size={12} /> Local Folder</>}
              </button>
            ))}
          </div>
          <NamespacePicker value={uploadNamespace} onChange={setUploadNamespace} namespaces={namespaces} />
          {addType === 'local_file' ? (
            <>
              <DropZone onFiles={(files) => { onIngest(files); setShowAdd(false) }} accept={uploadAccept ?? ".md,.txt,.py,.java,.ts,.js,.rs,.go,.html,.htm,.csv,.log,.json,.yaml,.yml,.sh,.rb,.c,.cpp,.h,.docx,.pdf"} />
              <IngestionProgress jobs={ingestionJobs} />
              <div className="text-[11px] text-muted bg-bg rounded border border-border p-2">
                Supports: Markdown, plain text, code files, HTML, JSON, YAML, CSV, DOCX, PDF. Max 50 MB per file.
                {acceptsNoExtension && ' Files with no extension (e.g. README) are ingested as plain text — drag-and-drop them here, since the file picker cannot list extensionless files.'}
              </div>
            </>
          ) : (
            <>
              <input value={addName} onChange={e => setAddName(e.target.value)} placeholder="Name (optional)"
                aria-label="Source name (optional)"
                className="w-full px-3 py-1.5 text-sm bg-bg rounded border border-border text-text" />
              <div className="flex gap-2">
                <input value={addUri} onChange={e => setAddUri(e.target.value)}
                  placeholder="Folder path (e.g., /home/user/notes)"
                  aria-label="Folder path"
                  className="flex-1 min-w-0 px-3 py-1.5 text-sm bg-bg rounded border border-border text-text" />
                {folderPickerAvailable && (
                  <button type="button" onClick={() => pickFolderMutation.mutate()} disabled={pickFolderMutation.isPending}
                    aria-label="Browse for a folder"
                    className="shrink-0 px-3 py-1.5 text-sm border border-border rounded text-text hover:bg-bg-elevated disabled:opacity-50 flex items-center gap-1">
                    <FolderOpen size={14} /> {pickFolderMutation.isPending ? 'Opening...' : 'Browse...'}
                  </button>
                )}
              </div>
              <textarea value={addIgnorePatterns} onChange={e => setAddIgnorePatterns(e.target.value)}
                placeholder="Ignore patterns (one per line, e.g. .trash/*)&#10;Templates/*"
                aria-label="Ignore patterns"
                rows={3} className="w-full px-3 py-1.5 text-sm bg-bg rounded border border-border text-text resize-none" />
              <div className="text-[11px] text-muted">Watches folder recursively. Supported files auto-ingested. Max 5,000 files per source.</div>
              <label htmlFor="sources-recursive" className="flex items-center gap-2 text-[12px] text-muted cursor-pointer">
                <input id="sources-recursive" aria-label="Include subdirectories (recursive)" type="checkbox" checked={addRecursive} onChange={e => setAddRecursive(e.target.checked)} className="accent-accent" />
                Include subdirectories (recursive)
              </label>
              <div className="flex gap-2 justify-end">
                <button onClick={() => setShowAdd(false)} className="px-3 py-1.5 text-xs border border-border rounded-md text-text">Cancel</button>
                <button onClick={handleAdd} disabled={addMutation.isPending || !addUri.trim()}
                  className="px-3 py-1.5 text-xs bg-accent text-accent-fg rounded-md disabled:opacity-50">{addMutation.isPending ? 'Adding...' : 'Add Folder'}</button>
              </div>
              {addMutation.isError && <div className="text-[12px] text-danger flex items-center gap-1"><AlertCircle size={12} /> {addMutation.error?.message || 'Failed to add source'}</div>}
            </>
          )}
        </div>
      )}

      {pendingConfirm && (
        <FolderConfirmDialog
          fileCount={pendingConfirm.fileCount}
          uri={pendingConfirm.uri}
          onConfirm={() => confirmMutation.mutate(pendingConfirm.id)}
          onCancel={() => {
            // Delete the pending source
            deleteMutation.mutate(pendingConfirm.id)
            setPendingConfirm(null)
          }}
          isPending={confirmMutation.isPending}
        />
      )}

      {!sources.length && !showAdd ? (
        <EmptyState icon={<FolderSync size={40} />} title="No sources registered" subtitle="Upload local files or watch a local folder to ingest documents" />
      ) : (
        sources.map(s => {
          const isDeleting = deleteMutation.isPending && deleteMutation.variables === s.id
          const isFolderType = s.source_type === 'local_folder' || s.source_type === 'obsidian_vault'
          const isExpanded = expandedSource === s.id
          const isPaused = s.sync_status === 'paused'
          const isPending = s.sync_status === 'pending_confirmation'
          return (
          <div key={s.id} className={`border border-border rounded-lg p-3 hover:border-border-strong transition-all ${isDeleting ? 'opacity-50' : ''}`}>
            {/* Stacks on narrow viewports: identity block on top, meta + actions below. */}
            <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3">
              <div className="flex items-start sm:items-center gap-2 sm:gap-3 min-w-0 flex-1">
              {isFolderType ? (
                <button onClick={() => setExpandedSource(isExpanded ? null : s.id)} className="text-muted shrink-0 mt-0.5 sm:mt-0"
                  aria-label={isExpanded ? 'Collapse folder details' : 'Expand folder details'}>
                  {isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                </button>
              ) : (
                <FolderSync size={16} className="text-muted shrink-0 mt-0.5 sm:mt-0" />
              )}
              <div className="flex-1 min-w-0">
                {editingId === s.id ? (
                  <div className="flex items-center gap-1">
                    <input autoFocus value={editDraft} onChange={e => setEditDraft(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter' && !renameMutation.isPending) submitRename(); else if (e.key === 'Escape') setEditingId(null) }}
                      maxLength={200} aria-label="Source name"
                      className="bg-bg-elevated border border-border rounded-md px-2 py-1 text-[13px] text-text outline-none w-full max-w-xs" />
                    <button aria-label="Save name" onClick={submitRename} disabled={renameMutation.isPending}
                      className="text-ok shrink-0 p-1 rounded hover:bg-bg-elevated disabled:opacity-50"><Check size={14} /></button>
                    <button aria-label="Cancel rename" onClick={() => setEditingId(null)}
                      className="text-muted shrink-0 p-1 rounded hover:bg-bg-elevated"><X size={14} /></button>
                  </div>
                ) : (
                  <div className="flex items-center gap-1 min-w-0 group/name">
                    <span className="text-sm font-medium text-text-strong truncate">{s.name}</span>
                    <button aria-label="Rename source" onClick={() => startRename(s)}
                      className="text-muted shrink-0 p-0.5 rounded opacity-100 sm:opacity-0 sm:group-hover/name:opacity-100 hover:text-text transition-opacity"><Pencil size={12} /></button>
                  </div>
                )}
                <div className="text-[11px] text-muted flex items-center gap-1.5 min-w-0">
                  <span className="truncate" title={s.uri || s.source_type}>
                    {s.source_type}{s.uri ? ` · ${s.uri}` : ''}
                  </span>
                  {s.source_type === 'local_file'
                    ? <span className="inline-flex items-center gap-0.5 text-ok shrink-0" title="Auto-watches for file changes every 5 min">● auto</span>
                    : isFolderType
                    ? <span className={`inline-flex items-center gap-0.5 shrink-0 ${isPaused ? 'text-warn' : isPending ? 'text-muted' : 'text-ok'}`} title={isPaused ? 'Paused' : isPending ? 'Awaiting confirmation' : 'Watching folder'}>● {isPaused ? 'paused' : isPending ? 'pending' : 'folder'}</span>
                    : <span className="inline-flex items-center gap-0.5 text-muted shrink-0" title="Use Sync button to update">○ manual</span>}
                </div>
                <SourceSummaryDisplay source={s} />
              </div>
              </div>
              {/* Meta + actions: wraps under the identity block on narrow viewports. */}
              <div className="flex items-center gap-2 sm:gap-3 flex-wrap sm:flex-nowrap shrink-0 pl-6 sm:pl-0">
              {isDeleting ? <Badge variant="warn">Deleting...</Badge> : (
                <Badge variant={s.sync_status === 'synced' || s.sync_status === 'active' ? 'ok' : s.sync_status === 'error' ? 'err' : s.sync_status === 'paused' ? 'warn' : 'aim'}>{s.sync_status}</Badge>
              )}
              <span className="text-[11px] text-muted whitespace-nowrap">{s.item_count ?? 0} items</span>
              {(() => { const { wordCount: wc } = parseSourceProps(s); if (!shouldShowWordCount(wc)) return null; return <span className="text-[11px] text-muted whitespace-nowrap">{wc! < 1000 ? `${wc} words` : `~${Math.round(wc! / 1000)}k words`}</span> })()}
              <StalenessIndicator lastSynced={s.last_synced} />
              {/* Pause/Resume/Confirm for folder sources */}
              {isFolderType && isPending && (
                <button aria-label="Confirm scan" onClick={() => confirmMutation.mutate(s.id)}
                  disabled={isDeleting || confirmMutation.isPending}
                  className="px-2 py-1 text-[11px] border border-accent rounded hover:bg-accent/10 text-accent disabled:opacity-50 flex items-center gap-1">
                  <CheckCircle size={12} /> Confirm
                </button>
              )}
              {isFolderType && !isPending && (
                isPaused ? (
                  <button aria-label="Resume scan" onClick={() => resumeMutation.mutate(s.id)} disabled={isDeleting}
                    className="px-2 py-1 text-[11px] border border-border rounded hover:bg-bg-elevated disabled:opacity-50 flex items-center gap-1">
                    <Play size={12} /> Resume
                  </button>
                ) : (
                  <button aria-label="Pause scan" onClick={() => pauseMutation.mutate(s.id)} disabled={isDeleting}
                    className="px-2 py-1 text-[11px] border border-border rounded hover:bg-bg-elevated disabled:opacity-50 flex items-center gap-1">
                    <Pause size={12} /> Pause
                  </button>
                )
              )}
              {!isFolderType && (
                <button aria-label="Sync source" onClick={() => syncSource(s.id)} disabled={isDeleting || syncingIds.has(s.id)}
                  className="px-2 py-1 text-[11px] border border-border rounded hover:bg-bg-elevated disabled:opacity-50 flex items-center gap-1">
                  {syncingIds.has(s.id)
                    ? <RefreshCw size={12} className="animate-spin" />
                    : <><RefreshCw size={12} /> Sync</>}
                </button>
              )}
              <button aria-label="Remove source" onClick={() => { if (confirm('Remove this source and all its ingested items?')) deleteMutation.mutate(s.id) }}
                disabled={isDeleting}
                className="px-2 py-1 text-[11px] border border-border rounded hover:bg-bg-elevated text-danger/70 hover:text-danger disabled:opacity-50 flex items-center gap-0.5">
                {isDeleting ? <RefreshCw size={12} className="animate-spin" /> : <X size={12} />}
              </button>
              </div>
            </div>
            {/* Inline expandable progress for folder sources */}
            {isFolderType && isExpanded && <FolderProgress sourceId={s.id} />}
          </div>
          )
        })
      )}
    </div>
  )
}
