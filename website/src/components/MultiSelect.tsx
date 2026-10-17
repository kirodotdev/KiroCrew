import { useMemo, useRef, useState } from 'react'
import { ChevronDown, Search } from 'lucide-react'

import { Btn, Checkbox, Input } from './ui'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'

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
  /** Told when the list opens or closes, e.g. to fix its order for one visit. */
  onOpenChange?: (open: boolean) => void
  /** One line at the top of the open list, for a reason that applies to every row. */
  notice?: string
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
  onOpenChange,
  notice,
}: Props) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
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
    <Popover open={open} onOpenChange={next => { setOpen(next); if (!next) setFilter(''); onOpenChange?.(next) }}>
      <PopoverTrigger
        id={id}
        disabled={disabled}
        aria-label={label}
        aria-haspopup="dialog"
        className="flex min-h-9 w-full items-center justify-between rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text outline-hidden transition-all hover:border-border-strong focus-visible:border-accent disabled:pointer-events-none disabled:opacity-40"
      >
        <span className="min-w-0 truncate text-left">{summary}</span>
        <ChevronDown className="lucide-inline ml-2 shrink-0 text-muted" aria-hidden />
      </PopoverTrigger>
      <PopoverContent
        align="start"
        onEscapeKeyDown={event => event.stopPropagation()}
        onKeyDown={handleKeyDown}
        // A bounded column: the search row and the notice keep their height and the
        // option list shrinks into what is left, so a notice that wraps on a narrow
        // screen can never push the last rows below the popover's clipped edge.
        className="flex w-[min(360px,calc(100vw-32px))] max-h-[360px] flex-col overflow-hidden p-0"
      >
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border p-2">
          <div className="flex min-w-[9rem] flex-1 items-center gap-2">
            <Search className="lucide-inline shrink-0 text-muted" aria-hidden />
            <Input
              ref={inputRef}
              autoFocus
              value={filter}
              onChange={event => setFilter(event.target.value)}
              placeholder={searchPlaceholder ?? i18nT('components.searchableSelect.search')}
              aria-label={searchPlaceholder ?? i18nT('components.searchableSelect.search')}
              className="min-w-0 flex-1 border-0 bg-transparent px-0 py-0 text-[13px] outline-hidden focus-ring placeholder:text-muted"
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
        {notice && (
          <div role="status" className="shrink-0 border-b border-border px-3 py-2 text-[12px] text-muted">
            {notice}
          </div>
        )}
        <div ref={listRef} role="group" aria-label={label} className="min-h-0 max-h-[300px] overflow-y-auto p-1">
          {filtered.length === 0 && (
            <div className="px-3 py-2 text-[13px] italic text-muted">
              {i18nT('components.searchableSelect.no_matches')}
            </div>
          )}
          {filtered.map(option => {
            const checked = selected.has(option.value)
            return (
              <label
                key={option.value}
                data-multi-select-option
                aria-disabled={option.locked || undefined}
                tabIndex={-1}
                className={`flex min-h-11 items-center gap-2 rounded-md px-2.5 py-1.5 transition-colors ${option.locked ? 'cursor-default' : 'cursor-pointer hover:bg-bg-hover'}`}
              >
                <Checkbox
                  tabIndex={-1}
                  checked={checked}
                  disabled={option.locked}
                  aria-label={option.label}
                  onChange={event => onToggle(option.value, event.target.checked)}
                />
                <span className="min-w-0 flex-1">
                  {/* Muted text, not an opacity drop, so a locked row reads as unavailable. */}
                  <span className={`block truncate text-[13px] font-medium ${option.locked ? 'text-muted' : 'text-text'}`}>{option.label}</span>
                  {option.description && <span className="block truncate text-[11px] text-muted">{option.description}</span>}
                </span>
              </label>
            )
          })}
        </div>
      </PopoverContent>
    </Popover>
  )
}
