import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, FileText, FolderOpen } from 'lucide-react'
import { Btn, Badge, ContentSkeleton } from '../../components/ui'
import { knowledgeApi } from './api'
import { ItemCard, FileSubGroup } from './ItemCard'
import type { KnowledgeItem, Source } from './types'

import { i18nT } from '../../i18n/t'
// Items fetched per expansion. The backend caps page size at 100
// (dashboard/handlers/knowledge.py list_items), so requesting more would
// silently truncate and break the in-group pager math.
export const GROUP_PAGE_SIZE = 100

// Sentinel source id for items that belong to no source. Mirrors the backend
// `_NO_SOURCE` constant used by /items?source_id= and /source-counts.
export const NO_SOURCE = '__none__'

export interface SourceGroupProps {
  /** Source id, or NO_SOURCE for the sourceless bucket. */
  sourceId: string
  source: Source | undefined
  /** Item count for this source under the active filters. */
  count: number
  onItemClick: (id: string) => void
  selectedItems: Set<string>
  onSelect: (id: string, checked: boolean) => void
  /** Filters currently applied to the list, forwarded to the scoped fetch. */
  filters: { type?: string; status?: string; namespace?: string }
  /**
   * Reports the items this group currently renders, or null when it renders
   * none (collapsed or unmounted). The page uses this to bound selection-wide
   * actions to visible items only: reading the query cache directly would also
   * pick up retained caches for groups that are no longer on screen, and a
   * bulk Delete would then destroy items the user cannot see.
   */
  onVisibleItemsChange?: (sourceId: string, items: KnowledgeItem[] | null) => void
  defaultOpen?: boolean
}

/**
 * One collapsed row per source. Items are fetched only when the row is
 * expanded, and the pager inside the row walks that source alone, so a large
 * source can never push a smaller one onto a later page of a shared pager.
 */
export function SourceGroup({
  sourceId, source, count, onItemClick, selectedItems, onSelect, filters,
  onVisibleItemsChange, defaultOpen = false,
}: SourceGroupProps) {
  const [open, setOpen] = useState(defaultOpen)
  const [page, setPage] = useState(1)

  const isNoSource = sourceId === NO_SOURCE
  const name = isNoSource ? 'No source' : (source?.name || 'Unknown source')
  const subtitle = isNoSource ? 'Items added directly' : (source?.uri || '')
  const isFolder = source?.source_type === 'local_folder' || source?.source_type === 'obsidian_vault'
  const isArtifact = source?.source_type === 'artifact'
  // Sub-group items: folder/vault sources group by file path; the aggregate
  // "artifact" source groups per-artifact (label = artifact name).
  const isGrouped = isFolder || isArtifact
  const Icon = isFolder ? FolderOpen : FileText

  // A filter change can shrink a source below the current page. The group stays
  // mounted (keyed by sourceId), so without this the user sees an empty page.
  useEffect(() => { setPage(1) }, [filters])

  const { data, isLoading } = useQuery({
    // Keyed under the shared 'knowledge-items' prefix so every existing
    // invalidateQueries(['knowledge-items']) call site reaches this cache too.
    queryKey: ['knowledge-items', 'source-items', sourceId, page, filters],
    queryFn: () => {
      const params = new URLSearchParams({
        source_id: sourceId,
        page: String(page),
        limit: String(GROUP_PAGE_SIZE),
      })
      if (filters.type) params.set('type', filters.type)
      if (filters.status) params.set('status', filters.status)
      if (filters.namespace) params.set('namespace', filters.namespace)
      return knowledgeApi<{ items: KnowledgeItem[]; total: number }>(`/items?${params}`)
    },
    // Lazy: nothing is requested until the user opens the row.
    enabled: open,
  })

  const items = useMemo(() => data?.items ?? [], [data])
  // Fall back to the badge count until the scoped fetch resolves, so the pager
  // does not flicker between 1 page and its real page count.
  const total = data?.total ?? count
  const totalPages = Math.ceil(total / GROUP_PAGE_SIZE)

  // Publish what is on screen, and retract it when this group stops rendering
  // items so a later select-all cannot reach it.
  useEffect(() => {
    if (!onVisibleItemsChange) return
    onVisibleItemsChange(sourceId, open ? items : null)
    return () => onVisibleItemsChange(sourceId, null)
  }, [onVisibleItemsChange, sourceId, open, items])

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
        aria-expanded={open}
        className="w-full flex items-center gap-2 px-3 py-2.5 bg-bg-elevated hover:bg-bg-hover text-left border-none cursor-pointer transition-colors"
      >
        {open ? <ChevronDown size={14} className="text-muted shrink-0" /> : <ChevronRight size={14} className="text-muted shrink-0" />}
        <Icon size={14} className={isFolder ? 'text-amber-500 shrink-0' : 'text-accent shrink-0'} />
        <span className="text-[13px] font-medium text-text-strong truncate">{name}</span>
        <Badge variant="ok">{count}</Badge>
        {source?.summary_topic && <span className="text-[11px] text-muted truncate max-w-[300px]">{source.summary_topic}</span>}
        {subtitle && <span className="text-[11px] text-muted truncate ml-auto max-w-[200px]">{subtitle}</span>}
      </button>
      {open && (
        <div className="space-y-1.5 p-2 pt-1">
          {isLoading && !items.length ? <ContentSkeleton /> : fileGroups ? (
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
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-3 pt-2 mt-1 border-t border-border">
              <Btn disabled={page <= 1} onClick={() => setPage(p => p - 1)}>{i18nT('pages.knowledge.sourceGroup.prev')}</Btn>
              <span className="text-[12px] text-text">{i18nT('pages.knowledge.sourceGroup.page')} {page} {i18nT('pages.knowledge.sourceGroup.of')} {totalPages}</span>
              <span className="text-[11px] text-muted">{i18nT('pages.knowledge.sourceGroup.items_count', { count: total })}</span>
              <Btn disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>{i18nT('pages.knowledge.sourceGroup.next')}</Btn>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default SourceGroup
