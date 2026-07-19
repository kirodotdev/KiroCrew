import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Plus, X, Zap } from 'lucide-react'
import type { ChatTag } from '../types'
import { api } from '../api/client'

export interface TagManagerListProps {
  /**
   * Governs the leading swatch:
   *   'manage'        — swatch is a static colour chip (no filter toggle). Used
   *                     by the header "Manage tags…" panel, where the list is a
   *                     pure tag CRUD surface with no column context.
   *   'column-filter' — swatch is an include/exclude checkbox that toggles the
   *                     owning board column's `tag_ids`. Requires `selectedIds`
   *                     (the column's current tag_ids) + `onToggleTag`, so the
   *                     board keeps mutating its column exactly as before.
   */
  mode: 'manage' | 'column-filter'
  /** Column's current tag_ids (column-filter mode only). */
  selectedIds?: string[]
  /** Called with the tag toggled and the resulting next id list (column-filter mode only). */
  onToggleTag?: (tagId: string, nextIds: string[]) => void
  /** data-testid for the "New tag" input. Board passes `tag-create-<colId>` for byte-identical behaviour. */
  createTestId?: string
}

/**
 * The tag-management list extracted verbatim from the board column-filter
 * popover: a scrollable list of tag rows (leading swatch · inline-rename input ·
 * status ⚡ toggle · delete ✕) plus a "New tag… ↵" create input. Self-contained
 * — it queries `['chat-tags']` and owns the create/update/delete mutations, so
 * every surface that renders it (board column popover, header Manage-tags panel)
 * shares one live source and stays in lockstep via the query cache. Row testids
 * (tag-row / tag-name / tag-status / tag-delete-<id>) are preserved so existing
 * board behaviour and selectors are unchanged.
 */
export default function TagManagerList({ mode, selectedIds = [], onToggleTag, createTestId = 'tag-create' }: TagManagerListProps) {
  const queryClient = useQueryClient()
  const { data: tags = [] } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags() })

  const createTagMutation = useMutation({
    mutationFn: ({ name, color, status }: { name: string; color?: string; status?: boolean }) => api.createChatTag(name, color, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
  })
  const updateTagMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; color?: string; status?: boolean } }) => api.updateChatTag(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
  })
  const deleteTagMutation = useMutation({
    mutationFn: (id: string) => api.deleteChatTag(id),
    onSuccess: () => {
      // Deleting a tag can prune column filters and un-tag slots, so refresh
      // those caches too (both board views react without a manual reload).
      queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
      queryClient.invalidateQueries({ queryKey: ['chat-slots'] })
    },
  })

  return (
    <>
      <div className="flex flex-col gap-0.5 max-h-[260px] overflow-y-auto" {...(mode === 'column-filter' ? { role: 'group', 'aria-label': 'Filter by tag' } : {})}>
        {[...tags].sort((a, b) => a.order - b.order).map(t => {
          const on = mode === 'column-filter' && selectedIds.includes(t.id)
          const nextIds = on ? selectedIds.filter(x => x !== t.id) : [...selectedIds, t.id]
          return (
            <div key={t.id} data-testid={`tag-row-${t.id}`} className={`group/tag flex items-center gap-1.5 px-1.5 py-1 rounded transition-all ${on ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}>
              {mode === 'column-filter' ? (
                /* Filter toggle — the colour swatch is the click target. role=checkbox
                 *  (not menuitemcheckbox) because the row lives in a form popover, not a
                 *  menu: a native <button> is Tab-reachable and Space/Enter-operable, and
                 *  the owning popover owns focus/Escape (no orphan menuitem ARIA). */
                <button type="button" role="checkbox" aria-checked={on} aria-label={`Include ${t.name} in filter`}
                  className="w-4 h-4 rounded-sm border border-border shrink-0 cursor-pointer relative outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  style={{ background: t.color }}
                  onClick={() => onToggleTag?.(t.id, nextIds)}>
                  {on && <span className="absolute inset-0 flex items-center justify-center" style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }}><Check size={10} /></span>}
                </button>
              ) : (
                /* Manage mode — static colour chip, no filter semantics */
                <span className="w-4 h-4 rounded-sm border border-border shrink-0" style={{ background: t.color }} aria-hidden />
              )}
              {/* Inline rename */}
              {/* key={t.name} remounts the uncontrolled input when the canonical
                *  name changes — so a rename in one rendered instance (board popover
                *  or the header Manage-tags panel) reflects in the other, which a
                *  bare defaultValue would not. */}
              <input
                key={t.name}
                type="text"
                data-testid={`tag-name-${t.id}`}
                aria-label={`Rename tag ${t.name}`}
                defaultValue={t.name}
                className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text py-0 px-0.5 rounded focus:bg-bg-elevated focus:border focus:border-accent/50"
                onBlur={e => { const v = e.target.value.trim(); if (!v) { e.target.value = t.name; return } if (v !== t.name) updateTagMutation.mutate({ id: t.id, body: { name: v } }) }}
                onKeyDown={e => {
                  const el = e.currentTarget as HTMLInputElement
                  if (e.key !== 'Enter' && e.key !== 'Escape') return
                  e.stopPropagation()
                  if (e.key === 'Escape') el.value = t.name
                  // Move focus to the row's first button (swatch in column-filter mode,
                  // status ⚡ in manage mode) instead of blur()ing to <body>. This still
                  // fires the input's onBlur (commit) but keeps focus inside the owning
                  // popover so its Tab-trap isn't defeated after a rename.
                  const sib = el.closest('[data-testid^="tag-row-"]')?.querySelector<HTMLElement>('button')
                  if (sib) sib.focus(); else el.blur()
                }}
                onClick={e => e.stopPropagation()}
              />
              {/* Status lightning — filled for status tags, muted ghost for non-status on hover */}
              <button type="button" data-testid={`tag-status-${t.id}`}
                className={`shrink-0 cursor-pointer bg-transparent border-none p-[2px] transition-all outline-none focus-visible:ring-1 focus-visible:ring-accent ${t.status ? 'text-accent hover:text-accent-hover' : 'text-transparent group-hover/tag:text-muted focus-visible:!text-muted hover:!text-text'}`}
                title={t.status ? 'Status tag (mutually exclusive on cards) — click to make regular' : 'Make status tag'}
                aria-pressed={!!t.status}
                aria-label={t.status ? `Remove status flag from ${t.name}` : `Make ${t.name} a status tag`}
                onClick={() => updateTagMutation.mutate({ id: t.id, body: { status: !t.status } })}>
                <Zap size={11} fill={t.status ? 'currentColor' : 'none'} />
              </button>
              {/* Delete */}
              <button type="button" data-testid={`tag-delete-${t.id}`}
                className="shrink-0 cursor-pointer bg-transparent border-none p-[2px] text-transparent group-hover/tag:text-muted focus-visible:!text-muted hover:!text-danger transition-all outline-none focus-visible:ring-1 focus-visible:ring-accent"
                title={`Delete tag "${t.name}"`}
                aria-label={`Delete tag ${t.name}`}
                onClick={() => { if (confirm(`Delete tag "${t.name}"?`)) deleteTagMutation.mutate(t.id) }}>
                <X size={11} />
              </button>
            </div>
          )
        })}
      </div>
      {/* Create new tag */}
      <div className="mt-2 border-t border-border pt-2 flex items-center gap-1.5">
        <span className="w-4 h-4 rounded-sm border border-dashed border-border shrink-0 flex items-center justify-center text-muted"><Plus size={10} /></span>
        <input
          type="text"
          data-testid={createTestId}
          placeholder="New tag… ↵"
          className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text py-0 px-0.5 placeholder:text-muted/60"
          onKeyDown={e => {
            if (e.key === 'Enter') {
              const el = e.currentTarget as HTMLInputElement
              const v = el.value.trim()
              if (!v) return
              createTagMutation.mutate({ name: v })
              el.value = ''
            }
          }}
          onClick={e => e.stopPropagation()}
        />
      </div>
    </>
  )
}
