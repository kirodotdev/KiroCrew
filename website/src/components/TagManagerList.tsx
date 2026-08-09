import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Plus, X, Zap } from 'lucide-react'
import type { ChatTag } from '../types'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
import { FOLDER_COLOR_PALETTE } from './folderColorCatalog'

function ColorSwatches({ value, onPick, label }: { value?: string; onPick: (color: string) => void; label: string }) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap pl-7 pt-1" role="radiogroup" aria-label={label}>
      {FOLDER_COLOR_PALETTE.map(({ value: color, label: colorLabel }) => (
        <button
          key={color}
          type="button"
          role="radio"
          aria-checked={value === color}
          aria-label={colorLabel()}
          title={colorLabel()}
          className={`w-4 h-4 rounded-full border cursor-pointer transition-transform hover:scale-110 ${value === color ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg border-transparent' : 'border-border'}`}
          style={{ background: color }}
          onClick={() => onPick(color)}
        />
      ))}
    </div>
  )
}
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
 * The tag-management list rendered by both the board column-filter popover and
 * the header Manage-tags panel: a scrollable list of tag rows (leading swatch ·
 * inline-rename input · status ⚡ toggle · delete ✕) plus a "New tag… ↵" create
 * input. Self-contained — it queries `['chat-tags']` and owns the
 * create/update/delete mutations, so every surface that renders it (board column
 * popover, header Manage-tags panel) shares one live source and stays in lockstep
 * via the query cache. Row testids (tag-row / tag-name / tag-status /
 * tag-delete-<id>) match the board's selectors.
 */
export default function TagManagerList({ mode, selectedIds = [], onToggleTag, createTestId = 'tag-create' }: TagManagerListProps) {
  const [openColorId, setOpenColorId] = useState<string | null>(null)
  const [newColor, setNewColor] = useState<string | undefined>(undefined)
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
      <div className="flex flex-col gap-0.5 max-h-[260px] overflow-y-auto" {...(mode === 'column-filter' ? { role: 'group', 'aria-label': i18nT('components.tagManagerList.filter_by_tag') } : {})}>
        {[...tags].sort((a, b) => a.order - b.order).map(t => {
          const on = mode === 'column-filter' && selectedIds.includes(t.id)
          const nextIds = on ? selectedIds.filter(x => x !== t.id) : [...selectedIds, t.id]
          return (
            <div key={t.id} data-testid={`tag-row-${t.id}`} className={`group/tag px-1.5 py-1 rounded transition-all ${on ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}>
              <div className="flex items-center gap-1.5">
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
                <button type="button" data-testid={`tag-color-${t.id}`} aria-label={i18nT('components.tagManagerList.change_color_for', { name: t.name })} title={i18nT('components.tagManagerList.change_color_for', { name: t.name })} className="w-4 h-4 rounded-full border border-border shrink-0 cursor-pointer hover:scale-110 transition-transform" style={{ background: t.color }} onClick={() => setOpenColorId(openColorId === t.id ? null : t.id)} />
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
                title={t.status ? i18nT('components.tagManagerList.status_tag_mutually_exclusive_on_cards_click_to') : i18nT('components.tagManagerList.make_status_tag')}
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
              {mode === 'manage' && openColorId === t.id && <ColorSwatches value={t.color} label={i18nT('components.tagManagerList.choose_color_for', { name: t.name })} onPick={color => { updateTagMutation.mutate({ id: t.id, body: { color } }); setOpenColorId(null) }} />}
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
          placeholder={i18nT('components.tagManagerList.new_tag')}
          className="flex-1 min-w-0 bg-transparent border-none outline-none text-[12px] text-text py-0 px-0.5 placeholder:text-muted/60"
          onKeyDown={e => {
            if (e.key === 'Enter') {
              const el = e.currentTarget as HTMLInputElement
              const v = el.value.trim()
              if (!v) return
              createTagMutation.mutate({ name: v, color: newColor })
              el.value = ''
              setNewColor(undefined)
            }
          }}
          onClick={e => e.stopPropagation()}
        />
      </div>
      <ColorSwatches value={newColor} label={i18nT('components.tagManagerList.choose_color_for_new_tag')} onPick={setNewColor} />
    </>
  )
}
