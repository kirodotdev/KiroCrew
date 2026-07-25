import { useState } from 'react'
import { Tag, Copy, ChevronDown, ChevronRight, FileText } from 'lucide-react'
import { Btn, Badge } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { typeBadgeVariant, formatDate, useCopy } from './helpers'
import type { KnowledgeItem } from './types'

export function ItemCard({ item, onClick, selected, onSelect }: {
  item: KnowledgeItem
  onClick: () => void
  selected: boolean
  onSelect: (checked: boolean) => void
}) {
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

export function FileSubGroup({ filePath, items, onItemClick, selectedItems, onSelect }: {
  filePath: string
  items: KnowledgeItem[]
  onItemClick: (id: string) => void
  selectedItems: Set<string>
  onSelect: (id: string, checked: boolean) => void
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
