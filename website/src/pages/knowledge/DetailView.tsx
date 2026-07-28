import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, Copy, Download, Archive, X, RefreshCw, AlertCircle, Link2, ArrowUpRight, Tag } from 'lucide-react'
import { Card, Btn, Badge, ContentSkeleton } from '../../components/ui'
import Clickable from '../../components/Clickable'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { knowledgeApi } from './api'
import { typeBadgeVariant, formatDate, useCopy } from './helpers'
import type { KnowledgeItem, Entity } from './types'

import { i18nT } from '../../i18n/t'
function highlightEntities(text: string, entities: Entity[], onEntityClick?: (name: string) => void) {
  if (!entities?.length) return text
  const names = [...entities].sort((a, b) => b.name.length - a.name.length).map(e => e.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const regex = new RegExp(`(${names.join('|')})`, 'g')
  const parts = text.split(regex)
  const nameSet = new Set(entities.map(e => e.name))
  return parts.map((part, i) => nameSet.has(part)
    ? <Clickable key={i} className="inline bg-accent/20 text-accent rounded px-0.5 cursor-pointer hover:bg-accent/30" onClick={() => onEntityClick?.(part)}>{part}</Clickable>
    : part)
}

function RelatedItems({ itemId, entities }: { itemId: string; entities: Entity[] }) {
  const { data: related = [] } = useQuery({
    queryKey: ['knowledge-related', itemId],
    queryFn: () => knowledgeApi<(KnowledgeItem & { shared_entities?: number })[]>(`/items/${itemId}/related`),
    enabled: (entities?.length ?? 0) > 0,
  })

  if (!related.length) return null

  return (
    <Card className="!mb-3">
      <div className="text-[13px] font-semibold text-text-strong mb-2">{i18nT('pages.knowledge.detailView.related_items')}{related.length})</div>
      <div className="space-y-1">
        {related.map(r => (
          <div key={r.id} className="text-[12px] text-text flex items-center gap-2">
            <Link2 size={10} className="text-accent shrink-0" />
            <span className="truncate flex-1">{r.title}</span>
            {r.shared_entities && <span className="text-[10px] text-muted">{r.shared_entities} {i18nT('pages.knowledge.detailView.shared')}</span>}
            <Badge variant={typeBadgeVariant(r.item_type)}>{r.item_type.replace(/_/g, ' ')}</Badge>
          </div>
        ))}
      </div>
    </Card>
  )
}

function TagEditor({ itemId, currentTags }: { itemId: string; currentTags: string }) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(currentTags)

  const saveMutation = useMutation({
    mutationFn: (tags: string) =>
      knowledgeApi(`/items/${itemId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-item', itemId] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      setEditing(false)
    },
  })

  if (!editing) {
    return (
      <div className="flex items-center gap-2 text-[12px]">
        <Tag size={11} className="text-muted" />
        {currentTags ? (
          <div className="flex flex-wrap gap-1">
            {(typeof currentTags === 'string' ? currentTags.split(',') : []).map((t, i) => (
              <span key={i} className="px-1.5 py-0.5 bg-bg-elevated border border-border rounded text-[11px] text-text">{t.trim()}</span>
            ))}
          </div>
        ) : <span className="text-muted">{i18nT('pages.knowledge.detailView.no_tags')}</span>}
        <button onClick={() => { setValue(currentTags); setEditing(true) }} className="text-accent text-[11px] bg-transparent border-none cursor-pointer hover:underline">{i18nT('pages.knowledge.detailView.edit')}</button>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <Tag size={11} className="text-muted shrink-0" />
      <input aria-label={i18nT('pages.knowledge.detailView.comma_separated_tags')} value={value} onChange={e => setValue(e.target.value)} placeholder={i18nT('pages.knowledge.detailView.tag1_tag2_tag3')}
        className="flex-1 px-2 py-1 text-[12px] bg-bg-elevated border border-border rounded outline-none"
        onKeyDown={e => { if (e.key === 'Enter') saveMutation.mutate(value); if (e.key === 'Escape') setEditing(false) }}
        autoFocus />
      <button onClick={() => saveMutation.mutate(value)} disabled={saveMutation.isPending}
        className="text-[11px] text-accent bg-transparent border-none cursor-pointer">{i18nT('pages.knowledge.detailView.save')}</button>
      <button onClick={() => setEditing(false)} className="text-[11px] text-muted bg-transparent border-none cursor-pointer">{i18nT('pages.knowledge.detailView.cancel')}</button>
    </div>
  )
}

function isMarkdownContent(item: KnowledgeItem): boolean {
  if (!item.tags) return false
  const tags = typeof item.tags === 'string' ? item.tags.split(',') : Array.isArray(item.tags) ? item.tags : []
  return tags.some(t => t.trim() === 'content_type:markdown')
}

export default function DetailView({ itemId, onBack, onEntityClick }: { itemId: string; onBack: () => void; onEntityClick?: (name: string) => void }) {
  const queryClient = useQueryClient()
  const { data: item, isLoading: loading } = useQuery({
    queryKey: ['knowledge-item', itemId],
    queryFn: () => knowledgeApi<KnowledgeItem>(`/items/${itemId}`),
  })

  const archiveMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      knowledgeApi(`/items/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) }),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ['knowledge-items'] })
      const prev = queryClient.getQueriesData({ queryKey: ['knowledge-items'] })
      queryClient.setQueriesData<{ items: KnowledgeItem[]; total: number }>({ queryKey: ['knowledge-items'] }, old =>
        old ? { ...old, items: old.items.filter(i => i.id !== itemId), total: old.total - 1 } : old
      )
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      ctx?.prev.forEach(([key, data]) => queryClient.setQueryData(key, data))
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-item', itemId] })
      onBack()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => knowledgeApi(`/items/${id}`, { method: 'DELETE' }),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ['knowledge-items'] })
      const prev = queryClient.getQueriesData({ queryKey: ['knowledge-items'] })
      queryClient.setQueriesData<{ items: KnowledgeItem[]; total: number }>({ queryKey: ['knowledge-items'] }, old =>
        old ? { ...old, items: old.items.filter(i => i.id !== itemId), total: old.total - 1 } : old
      )
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      ctx?.prev.forEach(([key, data]) => queryClient.setQueryData(key, data))
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-items'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge-stats'] })
      onBack()
    },
  })

  const { copied, copy } = useCopy()
  const copyContent = () => {
    if (!item) return
    copy(item.content || item.summary || item.title)
  }

  if (loading) return <ContentSkeleton />
  if (!item) return <div className="text-muted text-sm">{i18nT('pages.knowledge.detailView.item_not_found')}</div>

  return (
    <div className="animate-rise">
      <button onClick={onBack} className="flex items-center gap-1 text-muted hover:text-text text-[13px] mb-3 bg-transparent border-none cursor-pointer"><ChevronLeft size={14} /> {i18nT('pages.knowledge.detailView.back_to_list')}</button>
      <h2 className="text-lg font-bold text-text-strong mb-1">{item.title}</h2>
      <div className="flex items-center gap-2 mb-2">
        <Badge variant={typeBadgeVariant(item.item_type)}>{item.item_type.replace(/_/g, ' ')}</Badge>
        <span className="text-[11px] text-muted">{formatDate(item.updated_at)}</span>
        <span className={`text-[11px] ${item.status === 'active' ? 'text-ok' : 'text-muted'}`}>{item.status}</span>
        {item.namespace && item.namespace !== 'default' && <span className="bg-accent/10 text-accent px-1.5 py-0.5 rounded text-[10px]">{item.namespace}</span>}
      </div>

      <div className="mb-4">
        <TagEditor itemId={item.id} currentTags={item.tags || ''} />
      </div>

      {item.summary && (
        <Card className="!mb-3">
          <div className="text-[13px] font-semibold text-text-strong mb-1">{i18nT('pages.knowledge.detailView.summary')}</div>
          <div className="text-sm text-text whitespace-pre-wrap">{item.summary}</div>
          {item.source_locations?.map((loc, i) => (
            <div key={i} className="text-[11px] text-accent mt-1 flex items-center gap-0.5">
              <ArrowUpRight size={10} className="inline text-accent" /> {loc.section_title || loc.source_id} {loc.chunk_range && `(${loc.chunk_range})`}
            </div>
          ))}
        </Card>
      )}

      {!!item.entities?.length && (
        <Card className="!mb-3">
          <div className="text-[13px] font-semibold text-text-strong mb-2">{i18nT('pages.knowledge.detailView.entities')}{item.entities.length})</div>
          <div className="flex flex-wrap gap-1.5">
            {item.entities.map(e => (
              <Clickable key={e.id} onClick={() => onEntityClick?.(e.name)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-bg-elevated border border-border text-[12px] text-text cursor-pointer hover:border-accent">
                <span className="text-accent">{e.entity_type}</span> {e.name}
              </Clickable>
            ))}
          </div>
        </Card>
      )}

      {!!item.relations?.length && (
        <Card className="!mb-3">
          <div className="text-[13px] font-semibold text-text-strong mb-2">{i18nT('pages.knowledge.detailView.relations')}{item.relations.length})</div>
          <div className="space-y-1">
            {item.relations.map(r => (
              <div key={r.id} className="text-[12px] text-muted flex items-center gap-1">
                <Link2 size={10} className="text-accent" />
                <Clickable className="text-accent hover:underline cursor-pointer" onClick={() => onEntityClick?.(r.source_name || r.source_id)}>{r.source_name || r.source_id}</Clickable>
                <span className="text-text">{r.relation_type}</span>
                <Clickable className="text-accent hover:underline cursor-pointer" onClick={() => onEntityClick?.(r.target_name || r.target_id)}>{r.target_name || r.target_id}</Clickable>
              </div>
            ))}
          </div>
        </Card>
      )}

      <RelatedItems itemId={item.id} entities={item.entities || []} />

      <Card className="!mb-3">
        <div className="text-[13px] font-semibold text-text-strong mb-1">{i18nT('pages.knowledge.detailView.content')}</div>
        {isMarkdownContent(item) ? (
          <div className="text-[12px] text-text max-h-96 overflow-y-auto bg-bg-elevated rounded p-3 prose-sm">
            {/* MarkdownRenderer sanitizes rendered HTML output via rehypeSanitize plugin
               (MarkdownRenderer.tsx:192-210) — strips javascript:/data:/vbscript: URLs,
               event handler attributes, and dangerous tags (script/iframe/object/embed) */}
            <MarkdownRenderer content={item.content || ''} />
          </div>
        ) : (
          <pre className="text-[12px] text-text whitespace-pre-wrap max-h-96 overflow-y-auto font-mono bg-bg-elevated rounded p-3">{highlightEntities(item.content || '', item.entities || [], onEntityClick)}</pre>
        )}
        {isMarkdownContent(item) && !!item.entities?.length && (
          <div className="mt-2 flex flex-wrap gap-1 border-t border-border pt-2">
            <span className="text-[11px] text-muted mr-1">{i18nT('pages.knowledge.detailView.entities_in_this_chunk')}</span>
            {item.entities.map(e => (
              <Clickable key={e.id} onClick={() => onEntityClick?.(e.name)}
                className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-accent/10 text-accent text-[11px] cursor-pointer hover:bg-accent/20">
                {e.name}
              </Clickable>
            ))}
          </div>
        )}
      </Card>

      {item._score !== undefined && (
        <div className="text-[11px] text-muted mb-3">
          {i18nT('pages.knowledge.detailView.match')} {item._match_type} {i18nT('pages.knowledge.detailView.score')} {item._score.toFixed(3)})
        </div>
      )}

      <div className="flex gap-2 flex-wrap">
        <Btn onClick={copyContent}><Copy size={12} /> {copied ? 'Copied!' : 'Copy Content'}</Btn>
        <Btn onClick={() => { const a = document.createElement('a'); a.href = `/api/knowledge/items/${item.id}/export`; a.download = `${item.title}.knowledge`; a.click() }}><Download size={12} /> {i18nT('pages.knowledge.detailView.export')}</Btn>
        {item.status === 'archived'
          ? <Btn disabled={archiveMutation.isPending} onClick={() => archiveMutation.mutate({ id: item.id, status: 'active' })}><RefreshCw size={12} /> {i18nT('pages.knowledge.detailView.unarchive')}</Btn>
          : <Btn disabled={archiveMutation.isPending} onClick={() => archiveMutation.mutate({ id: item.id, status: 'archived' })}><Archive size={12} /> {i18nT('pages.knowledge.detailView.archive')}</Btn>}
        <Btn disabled={deleteMutation.isPending} onClick={() => { if (confirm(i18nT('pages.knowledge.detailView.permanently_delete_this_item'))) deleteMutation.mutate(item.id) }}><X size={12} /> {i18nT('pages.knowledge.detailView.delete')}</Btn>
      </div>
      {(archiveMutation.isError || deleteMutation.isError) && (
        <div className="mt-2 text-[12px] text-danger flex items-center gap-1">
          <AlertCircle size={12} /> {(archiveMutation.error || deleteMutation.error)?.message || 'Action failed'}
        </div>
      )}
    </div>
  )
}
