import { useMemo, useRef, useState } from 'react'
import { ChevronDown, GripVertical, Search } from 'lucide-react'
import { DndContext, closestCenter, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy, useSortable, arrayMove } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

import { Btn, Checkbox, Input } from './ui'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'

import { useDndSensors } from '../hooks/useDndSensors'
import { i18nT } from '../i18n/t'

export interface MultiSelectOption {
  value: string
  label: string
  description?: string
  locked?: boolean
}

interface Props {
  options: MultiSelectOption[]
  selected: ReadonlySet<string>
  onToggle: (value: string, selected: boolean) => void
  bulkActions?: ReadonlyArray<{ label: string; onSelect: () => void }>
  summary: string
  label: string
  searchPlaceholder?: string
  disabled?: boolean
  id?: string
  /** When set, rows grow a drag grip and the list is user-orderable: called
   *  with the values of every NON-LOCKED option in their new display order
   *  after a drag or keyboard move. Locked options are pinned before the
   *  sortable region and never move. Reordering is suspended while the search
   *  filter is active — a filtered list is a subsequence, so a drop there has
   *  no well-defined position in the full order. */
  onReorder?: (orderedValues: string[]) => void
  /** Grips render disabled (rows stay toggleable) — e.g. while the saved
   *  order has not loaded yet or the model list is degraded, when a write
   *  could clobber state the client has not seen. */
  reorderDisabled?: boolean
  /** Accessible label for a row's grip, e.g. "Reorder {{model}}". */
  reorderRowLabel?: (value: string) => string
  /** One action rendered on its own row under the list — for an action that
   *  must not join the bulk-actions row (max-two-buttons-per-row) and wants
   *  more visual weight than a third header chip, e.g. "Reset to default
   *  order". */
  footerAction?: { label: string; onSelect: () => void; disabled?: boolean }
}

export default function MultiSelect({
  options,
  selected,
  onToggle,
  bulkActions,
  summary,
  label,
  searchPlaceholder,
  disabled,
  id,
  onReorder,
  reorderDisabled,
  reorderRowLabel,
  footerAction,
}: Props) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const sensors = useDndSensors({ distance: 4, keyboard: true })
  const filtering = filter.trim().length > 0
  // Reordering is only live on the UNFILTERED list (see the prop doc) and
  // only over non-locked rows; locked rows are pinned before the region.
  const sortable = !!onReorder && !filtering && !reorderDisabled
  const sortableValues = useMemo(
    () => options.filter(o => !o.locked).map(o => o.value),
    [options],
  )
  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id || !onReorder) return
    const from = sortableValues.indexOf(String(active.id))
    const to = sortableValues.indexOf(String(over.id))
    if (from < 0 || to < 0) return
    onReorder(arrayMove(sortableValues, from, to))
  }
  const filtered = useMemo(() => {
    const tokens = filter.trim().toLowerCase().split(/\s+/).filter(Boolean)
    if (!tokens.length) return options
    return options.filter(option => {
      const haystack = `${option.label} ${option.value} ${option.description ?? ''}`.toLowerCase()
      return tokens.every(token => haystack.includes(token))
    })
  }, [filter, options])

  const optionRows = () =>
    Array.from(listRef.current?.querySelectorAll<HTMLElement>('[data-multi-select-option]') ?? [])

  const handleKeyDown = (event: React.KeyboardEvent) => {
    // dnd-kit's keyboard sensor owns the grip: Space lifts, arrows move,
    // Space drops. Intercepting arrows here would fight the sensor — the
    // same seam ModelEffortDropdown used for its drill-in pages.
    if ((event.target as HTMLElement | null)?.closest('[data-reorder-grip]')) return
    const rows = optionRows()
    const active = document.activeElement as HTMLElement | null
    const index = active ? rows.indexOf(active) : -1
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      event.stopPropagation()
      ;(rows[index + 1] ?? rows[rows.length - 1])?.focus()
      return
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      event.stopPropagation()
      if (index <= 0) inputRef.current?.focus()
      else rows[index - 1]?.focus()
      return
    }
    if (event.key === 'Home' && index >= 0) {
      event.preventDefault()
      rows[0]?.focus()
      return
    }
    if (event.key === 'End' && index >= 0) {
      event.preventDefault()
      rows[rows.length - 1]?.focus()
      return
    }
    if ((event.key === 'Enter' || event.key === ' ') && index >= 0) {
      event.preventDefault()
      const option = filtered[index]
      if (option && !option.locked) onToggle(option.value, !selected.has(option.value))
    }
  }

  return (
    <Popover open={open} onOpenChange={next => { setOpen(next); if (!next) setFilter('') }}>
      <PopoverTrigger
        id={id}
        disabled={disabled}
        aria-label={label}
        aria-haspopup="dialog"
        className="flex min-h-9 w-full items-center justify-between rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text outline-none transition-all hover:border-border-strong focus-visible:border-accent disabled:pointer-events-none disabled:opacity-40"
      >
        <span className="min-w-0 truncate text-left">{summary}</span>
        <ChevronDown className="lucide-inline ml-2 shrink-0 text-muted" aria-hidden />
      </PopoverTrigger>
      <PopoverContent
        align="start"
        onEscapeKeyDown={event => event.stopPropagation()}
        onKeyDown={handleKeyDown}
        className="w-[min(360px,calc(100vw-32px))] max-h-[360px] overflow-hidden p-0"
      >
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-2">
          <div className="flex min-w-[9rem] flex-1 items-center gap-2">
            <Search className="lucide-inline shrink-0 text-muted" aria-hidden />
            <Input
              ref={inputRef}
              autoFocus
              value={filter}
              onChange={event => setFilter(event.target.value)}
              placeholder={searchPlaceholder ?? i18nT('components.searchableSelect.search')}
              aria-label={searchPlaceholder ?? i18nT('components.searchableSelect.search')}
              className="min-w-0 flex-1 border-0 bg-transparent px-0 py-0 text-[13px] outline-none focus-ring placeholder:text-muted"
            />
          </div>
          {!!bulkActions?.length && (
            <div className="flex shrink-0 items-center gap-1">
              {bulkActions.map(action => (
                <Btn key={action.label} type="button" onClick={action.onSelect} className="border-0 px-1.5 py-0.5 text-[11px]">
                  {action.label}
                </Btn>
              ))}
            </div>
          )}
        </div>
        <div ref={listRef} role="group" aria-label={label} className="max-h-[300px] overflow-y-auto p-1">
          {filtered.length === 0 && (
            <div className="px-3 py-2 text-[13px] italic text-muted">
              {i18nT('components.searchableSelect.no_matches')}
            </div>
          )}
          {sortable ? (
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
              <SortableContext items={sortableValues} strategy={verticalListSortingStrategy}>
                {filtered.map(option =>
                  option.locked ? (
                    <OptionRow key={option.value} option={option} checked={selected.has(option.value)} onToggle={onToggle} />
                  ) : (
                    <SortableOptionRow
                      key={option.value}
                      option={option}
                      checked={selected.has(option.value)}
                      onToggle={onToggle}
                      gripLabel={reorderRowLabel?.(option.value) ?? option.label}
                    />
                  ),
                )}
              </SortableContext>
            </DndContext>
          ) : (
            filtered.map(option => (
              <OptionRow
                key={option.value}
                option={option}
                checked={selected.has(option.value)}
                onToggle={onToggle}
                gripPlaceholder={!!onReorder}
                gripDisabledReason={
                  onReorder
                    ? filtering
                      ? i18nT('components.multiSelect.reorder_clear_search')
                      : i18nT('components.multiSelect.reorder_unavailable')
                    : undefined
                }
              />
            ))
          )}
        </div>
        {footerAction && (
          <div className="border-t border-border p-1">
            <Btn
              type="button"
              onClick={footerAction.onSelect}
              disabled={footerAction.disabled}
              className="w-full border-0 px-2.5 py-1.5 text-[12px] text-muted hover:text-text"
            >
              {footerAction.label}
            </Btn>
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

function OptionRow({
  option,
  checked,
  onToggle,
  gripPlaceholder,
  gripDisabledReason,
}: {
  option: MultiSelectOption
  checked: boolean
  onToggle: (value: string, selected: boolean) => void
  /** Keeps row geometry stable when siblings show grips (locked rows). */
  gripPlaceholder?: boolean
  /** Renders a DISABLED grip with this tooltip instead of hiding it — grips
   *  vanishing while a filter is active reads as breakage (UX review), so the
   *  affordance stays visible and explains itself. */
  gripDisabledReason?: string
}) {
  return (
    <label
      data-multi-select-option
      aria-disabled={option.locked || undefined}
      tabIndex={-1}
      className={`flex min-h-11 items-center gap-2 rounded-md px-2.5 py-1.5 transition-colors ${option.locked ? 'cursor-default opacity-70' : 'cursor-pointer hover:bg-bg-hover'}`}
    >
      {gripDisabledReason && !option.locked ? (
        <span
          aria-hidden
          title={gripDisabledReason}
          className="flex w-4 shrink-0 cursor-not-allowed items-center justify-center text-muted opacity-40"
        >
          <GripVertical size={14} />
        </span>
      ) : (
        gripPlaceholder && <span aria-hidden className="w-4 shrink-0" />
      )}
      <Checkbox
        tabIndex={-1}
        checked={checked}
        disabled={option.locked}
        aria-label={option.label}
        onChange={event => onToggle(option.value, event.target.checked)}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium text-text">{option.label}</span>
        {option.description && <span className="block truncate text-[11px] text-muted">{option.description}</span>}
      </span>
    </label>
  )
}

/** A reorderable row: OptionRow plus a dnd-kit grip. Mirrors ModelOrderEditor's
 *  former SortableModelRow (drag by grip only, so the checkbox stays a plain
 *  click target; keyboard reorder = focus grip, Space, arrows, Space). */
function SortableOptionRow({
  option,
  checked,
  onToggle,
  gripLabel,
}: {
  option: MultiSelectOption
  checked: boolean
  onToggle: (value: string, selected: boolean) => void
  gripLabel: string
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: option.value })
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={isDragging ? 'relative z-10 opacity-80' : undefined}
    >
      <label
        data-multi-select-option
        tabIndex={-1}
        className="flex min-h-11 items-center gap-2 rounded-md px-2.5 py-1.5 transition-colors cursor-pointer hover:bg-bg-hover"
      >
        <button
          type="button"
          data-reorder-grip
          aria-label={gripLabel}
          className="flex w-4 shrink-0 cursor-grab items-center justify-center border-0 bg-transparent p-0 text-muted hover:text-text focus-ring"
          {...attributes}
          {...listeners}
        >
          <GripVertical size={14} aria-hidden />
        </button>
        <Checkbox
          tabIndex={-1}
          checked={checked}
          disabled={option.locked}
          aria-label={option.label}
          onChange={event => onToggle(option.value, event.target.checked)}
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-text">{option.label}</span>
          {option.description && <span className="block truncate text-[11px] text-muted">{option.description}</span>}
        </span>
      </label>
    </div>
  )
}
