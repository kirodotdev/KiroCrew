import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Compass, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { Card, Btn, SearchInput, EmptyState } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import Modal from '../../components/Modal'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import type { SteeringFile, SteeringList } from '../../types'

const SOURCE_LABEL: Record<string, string> = { user: 'Global', workspace: 'Workspace' }

const NEW_TEMPLATE = '# Title\n\nDescribe the convention the agent should always follow.\n'

/** Textarea styling matches SkillForm's raw-markdown editor. */
const EDITOR_CLASS =
  'w-full h-full min-h-[320px] bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-none focus-ring'

export default function SteeringTab() {
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newSource, setNewSource] = useState<'user' | 'workspace'>('workspace')
  const [newBody, setNewBody] = useState(NEW_TEMPLATE)

  const { data, isLoading, isFetching, refetch } = useQuery<SteeringList>({
    queryKey: ['steering'],
    queryFn: () => api.steeringFiles(),
  })
  const files = useMemo(() => data?.files ?? [], [data])
  const roots = useMemo(() => data?.roots ?? [], [data])
  const hasProject = !!data?.project

  const { data: detail } = useQuery({
    queryKey: ['steering-file', selectedKey],
    queryFn: () => api.steeringFile(selectedKey!),
    enabled: !!selectedKey,
  })

  const createFile = useMutation({
    mutationFn: (body: { name: string; content: string; source: string }) =>
      api.createSteering(body.name, body.content, body.source),
    onSuccess: (res: { key?: string }) => {
      setCreating(false)
      setNewName('')
      setNewBody(NEW_TEMPLATE)
      if (res?.key) {
        setSelectedKey(res.key)
        // Drop any cached detail for this key: a file deleted and recreated
        // under the same name would otherwise populate the editor from the
        // OLD file's retained cache entry (gcTime keeps it, and it is served
        // stale on re-select), and saving that would overwrite the new file.
        queryClient.removeQueries({ queryKey: ['steering-file', res.key] })
      }
      queryClient.invalidateQueries({ queryKey: ['steering'] })
    },
  })

  const updateFile = useMutation({
    mutationFn: ({ key, content }: { key: string; content: string }) => api.updateSteering(key, content),
    onSuccess: () => {
      setEditing(false)
      queryClient.invalidateQueries({ queryKey: ['steering'] })
      queryClient.invalidateQueries({ queryKey: ['steering-file'] })
    },
  })

  const deleteFile = useMutation({
    mutationFn: (key: string) => api.deleteSteering(key),
    onSuccess: (_res, key) => {
      setSelectedKey(null)
      setEditing(false)
      // Remove, not invalidate: the file is gone, so its cached detail must
      // not survive to seed a later file created under the same key.
      queryClient.removeQueries({ queryKey: ['steering-file', key] })
      queryClient.invalidateQueries({ queryKey: ['steering'] })
    },
  })

  const mutError = (createFile.error || updateFile.error || deleteFile.error) as Error | null

  const filtered = useMemo(() => {
    const q = filter.toLowerCase()
    if (!q) return files
    return files.filter(f => (f.key + ' ' + (f.description || '')).toLowerCase().includes(q))
  }, [files, filter])

  const selected = useMemo(() => files.find(f => f.key === selectedKey) ?? null, [files, selectedKey])

  // Keep a valid selection; suspended while editing so an unsaved draft is
  // never discarded by a background refetch reordering the list.
  useEffect(() => {
    if (editing) return
    if (filtered.length === 0) { if (selectedKey !== null) setSelectedKey(null); return }
    if (!selectedKey || !filtered.some(f => f.key === selectedKey)) setSelectedKey(filtered[0].key)
  }, [filtered, selectedKey, editing])

  // Default the create dialog to the scope that exists.
  useEffect(() => { setNewSource(hasProject ? 'workspace' : 'user') }, [hasProject])

  const select = (f: SteeringFile) => { setSelectedKey(f.key); setEditing(false) }

  const renderRow = (f: SteeringFile) => {
    const isSel = f.key === selectedKey
    return (
      <div
        key={f.key}
        role="button"
        tabIndex={0}
        aria-current={isSel ? 'true' : undefined}
        aria-label={`Select ${f.rel}`}
        onClick={() => select(f)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(f) } }}
        className={`flex flex-col gap-0.5 px-3 py-2.5 rounded-md cursor-pointer mb-1 transition-colors ${
          isSel ? 'list-selected bg-accent-subtle' : 'bg-bg-elevated hover:bg-bg-hover'
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[13px] font-semibold text-text truncate flex-1">{f.rel}</span>
          <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-bg-elevated text-muted border border-border font-bold shrink-0">
            {SOURCE_LABEL[f.source] || f.source}
          </span>
        </div>
        {f.description && <div className="text-[11px] text-muted truncate">{f.description}</div>}
      </div>
    )
  }

  const rootHint = roots.map(r => r.path).join('  ·  ')

  return (<>
    <Modal
      open={creating}
      onClose={() => setCreating(false)}
      title="New steering file"
      maxWidth={640}
      footer={<>
        <Btn onClick={() => setCreating(false)}>Cancel</Btn>
        <Btn
          primary
          disabled={!newName.trim() || !newBody.trim() || createFile.isPending}
          onClick={() => createFile.mutate({ name: newName.trim(), content: newBody, source: newSource })}
        >Create</Btn>
      </>}
    >
      <div className="flex flex-col gap-3">
        <label className="flex flex-col gap-1" htmlFor="steering-new-name">
          <span className="text-[13px] text-muted">File name</span>
          <input
            id="steering-new-name"
            aria-label="Steering file name"
            className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[13px] outline-none focus-ring"
            placeholder="api-standards.md"
            value={newName}
            onChange={e => setNewName(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1" htmlFor="steering-new-scope">
          <span className="text-[13px] text-muted">Scope</span>
          <select
            id="steering-new-scope"
            className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[13px] outline-none focus-ring"
            value={newSource}
            onChange={e => setNewSource(e.target.value as 'user' | 'workspace')}
          >
            <option value="workspace" disabled={!hasProject}>
              Workspace — this project only{hasProject ? '' : ' (no project set)'}
            </option>
            <option value="user">Global — every project</option>
          </select>
        </label>
        <label className="flex flex-col gap-1" htmlFor="steering-new-body">
          <span className="text-[13px] text-muted">Content</span>
          <textarea
            id="steering-new-body"
            aria-label="Steering file content"
            className={EDITOR_CLASS}
            rows={14}
            value={newBody}
            onChange={e => setNewBody(e.target.value)}
          />
        </label>
      </div>
    </Modal>

    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">
      Steering ({files.length})
      <InfoTip text="Always-on markdown conventions injected into every session. Global files live in ~/.kiro/steering and apply everywhere; workspace files live in <project>/.kiro/steering and apply to that project only." />
      <span className="ml-auto">
        <Btn primary onClick={() => setCreating(true)}>New Steering File</Btn>
      </span>
    </h4>
    <Card>
      <div className="flex items-center gap-2 mb-3">
        <div className="relative max-w-[480px] flex-1">
          <SearchInput placeholder="Filter steering files…" value={filter} onChange={e => setFilter(e.target.value)} />
          {filter && (
            <button
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer"
              onClick={() => setFilter('')}
              aria-label="Clear search"
            >&times;</button>
          )}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Btn onClick={() => refetch()} disabled={isFetching} aria-label="Refresh steering files">
            <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} />
          </Btn>
        </div>
      </div>

      {mutError && (
        <div className="mb-3 px-3 py-2 rounded-md bg-danger/10 border border-danger/20 text-[13px] text-danger">
          {mutError.message}
        </div>
      )}

      {isLoading ? (
        <div className="flex gap-3 h-[calc(100vh-260px)] min-h-[420px]">
          <div className="w-[240px] shrink-0 space-y-1">{Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-[52px] rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5, animationDelay: `${i * 80}ms` }} />
          ))}</div>
          <div className="flex-1 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.3 }} />
        </div>
      ) : files.length === 0 ? (
        <EmptyState
          icon={<Compass className="lucide-inline" />}
          title="No steering files yet"
          subtitle={`Steering files are always-on markdown conventions. Looked in: ${rootHint || '~/.kiro/steering'}`}
        />
      ) : (
        <div className="flex gap-3 h-[calc(100vh-260px)] min-h-[420px]">
          <div className="w-[240px] shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md p-2" role="listbox" aria-label="Steering files">
            {filtered.map(renderRow)}
            {filtered.length === 0 && <div className="text-muted/70 text-[12px] italic px-2 py-2">No files match “{filter}”.</div>}
          </div>

          <div className="flex-1 min-w-0 flex flex-col border border-border rounded-md bg-card overflow-hidden">
            {!selected ? (
              <div className="flex items-center justify-center h-full text-muted text-[13px]">Select a steering file to view it</div>
            ) : (
              <div className="flex flex-col h-full min-h-0">
                <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-border shrink-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-bold text-text-strong truncate">{selected.rel}</span>
                    <span className="text-[11px] px-1.5 py-[1px] rounded-full bg-bg-elevated text-muted border border-border font-bold shrink-0">
                      {SOURCE_LABEL[selected.source] || selected.source}
                    </span>
                    <span className="text-[11px] text-muted font-mono truncate">{selected.path}</span>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    {editing ? (<>
                      <Btn onClick={() => setEditing(false)}>Cancel</Btn>
                      <Btn primary disabled={!draft.trim() || updateFile.isPending} onClick={() => updateFile.mutate({ key: selected.key, content: draft })}>Save</Btn>
                    </>) : (<>
                      <Btn disabled={detail === undefined} onClick={() => { setDraft(detail?.content ?? ''); setEditing(true) }}>Edit</Btn>
                      <Btn danger onClick={() => { if (confirm(`Delete "${selected.rel}"?`)) deleteFile.mutate(selected.key) }}>Delete</Btn>
                    </>)}
                  </div>
                </div>
                <div className="flex-1 min-h-0 overflow-y-auto p-4">
                  {editing
                    ? <textarea className={EDITOR_CLASS} aria-label={`Edit ${selected.rel}`} value={draft} onChange={e => setDraft(e.target.value)} />
                    : detail === undefined
                      ? <div className="text-muted text-[13px]">Loading…</div>
                      : <div className="text-sm leading-relaxed"><MarkdownRenderer content={detail.content} /></div>}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  </>)
}
