import { useCallback, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'

import { i18nT } from '../i18n/t'

/**
 * Searchable single-select dropdown for lists too long to scan.
 *
 * Sibling of `SimpleSelect`: same "one value in, one value out" contract, but
 * the popup carries a filter box. Reach for `SimpleSelect` (Radix Select) at a
 * dozen-ish options and this one past that — Radix Select's popup caps at 240px
 * with nothing but first-letter typeahead, which stops scaling somewhere around
 * the IANA timezone list.
 *
 * Built on Radix Popover rather than hand-rolled portal positioning so it
 * inherits popper flipping, scroll following, focus return and DismissableLayer
 * nesting. Popover has no option semantics of its own, so the listbox ARIA and
 * roving focus come from `useListboxKeyboard` — the same hook AgentSelector
 * uses, which is deliberately Radix-free and composes either way.
 *
 * The trigger and rows reuse `ui/select.tsx`'s class strings verbatim, so a
 * SimpleSelect and a SearchableSelect sitting in one panel look identical.
 */

export interface SearchableSelectOption {
  value: string
  label: string
  /** Muted secondary line, e.g. a timezone's UTC offset. */
  sublabel?: string
  /** Extra text the filter matches but never displays. */
  keywords?: string
  disabled?: boolean
}

export interface SearchableSelectProps {
  options: SearchableSelectOption[]
  value: string
  onChange: (value: string) => void
  /** Trigger text when `value` matches no option (legacy or stale values). */
  triggerFallback?: string
  /** Filter-box placeholder. Defaults to a generic "Search…". */
  searchPlaceholder?: string
  disabled?: boolean
  /** Set on the trigger so an external <label htmlFor> can address it. */
  id?: string
  className?: string
  style?: React.CSSProperties
  'aria-label'?: string
}

/** Case-insensitive AND-match over every whitespace-separated token, so
 *  "asia shang" and "shang asia" both land on Asia/Shanghai. */
function matches(opt: SearchableSelectOption, tokens: string[]): boolean {
  if (!tokens.length) return true
  const hay = `${opt.label} ${opt.sublabel ?? ''} ${opt.value} ${opt.keywords ?? ''}`.toLowerCase()
  return tokens.every(t => hay.includes(t))
}

export default function SearchableSelect({
  options,
  value,
  onChange,
  triggerFallback,
  searchPlaceholder,
  disabled,
  id,
  className,
  style,
  'aria-label': ariaLabel,
}: SearchableSelectProps) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const triggerRef = useRef<HTMLButtonElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const selected = options.find(o => o.value === value)

  const filtered = useMemo(() => {
    const tokens = filter.trim().toLowerCase().split(/\s+/).filter(Boolean)
    return options.filter(o => matches(o, tokens))
  }, [options, filter])

  // Radix returns focus to the trigger itself on close, so this only has to
  // flip the state; keeping the name matches the hook's contract.
  const closeToTrigger = useCallback(() => setOpen(false), [])

  const choose = useCallback((opt: SearchableSelectOption) => {
    if (opt.disabled) return
    onChange(opt.value)
    setOpen(false)
  }, [onChange])

  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef: listRef,
    inputRef,
    // Radix autofocuses the first focusable node in the content, which is the
    // filter box — so the hook must not also grab focus for the list.
    hasFilterInput: true,
    // `useListboxKeyboard` gates its Enter branch on `filteredCount === 1`, but
    // combobox convention commits the top match whenever the list is non-empty:
    // typing "los ang" and pressing Enter should not sit silent just because two
    // rows still match. Reporting 1 whenever anything matches routes Enter here,
    // and `choose(filtered[0])` picks the row the user is looking at.
    filteredCount: filtered.length > 0 ? 1 : 0,
    onEnterSingleMatch: () => { const o = filtered[0]; if (o) choose(o) },
    closeToTrigger,
  })

  return (
    <Popover
      open={open}
      onOpenChange={o => { setOpen(o); if (!o) setFilter('') }}
    >
      <PopoverTrigger
        ref={triggerRef}
        id={id}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        className={[
          'flex items-center justify-between w-full px-3 py-2 rounded-md text-sm border border-border bg-bg-elevated text-text',
          'hover:border-border-strong transition-all cursor-pointer outline-none',
          'focus-visible:border-accent disabled:opacity-40 disabled:pointer-events-none',
          className || '',
        ].join(' ').trim()}
        style={style}
      >
        <span className="truncate text-left min-w-0">
          {selected
            ? (selected.sublabel ? `${selected.label} (${selected.sublabel})` : selected.label)
            : (triggerFallback ?? value ?? '—')}
        </span>
        <ChevronDown className="ml-2 shrink-0 text-muted" size={14} aria-hidden />
      </PopoverTrigger>
      <PopoverContent
        align="start"
        // Escape must dismiss ONLY this popup. Radix dismisses from a
        // document-level listener, so without stopping propagation the same
        // keydown reaches window-level Escape handlers and closes the host
        // surface too — the fix mirrors ui/select.tsx's SelectContent.
        onEscapeKeyDown={e => e.stopPropagation()}
        // Exactly the trigger's width — see the note in ui/select.tsx's
        // SelectContent. No `min-w` floor: it would overhang the trigger.
        className="w-[var(--radix-popover-trigger-width)] max-h-[300px] p-0 flex flex-col overflow-hidden"
      >
        <div className="p-2 border-b border-border flex items-center gap-2">
          <Search size={13} className="text-muted shrink-0" aria-hidden />
          <input
            ref={inputRef}
            type="text"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            onKeyDown={e => {
              // An IME confirms a composition with Enter. Letting that through
              // would commit `filtered[0]` and close the picker instead of
              // accepting the composed text — so every CJK user typing into this
              // box would lose their input on the first Enter. `isComposing` is
              // true for exactly the keystrokes the IME owns.
              if (e.nativeEvent.isComposing) return
              onListKeyDown(e)
            }}
            placeholder={searchPlaceholder ?? i18nT('components.searchableSelect.search')}
            aria-label={searchPlaceholder ?? i18nT('components.searchableSelect.search')}
            className="flex-1 min-w-0 bg-transparent border-0 outline-none text-[13px] text-text placeholder:text-muted"
          />
        </div>
        <div
          ref={listRef}
          role="listbox"
          aria-label={ariaLabel}
          // Roving focus lives on the option buttons; the container is only
          // programmatically focusable so the interactive role is reachable.
          tabIndex={-1}
          onKeyDown={onListKeyDown}
          className="flex-1 min-h-0 overflow-y-auto p-1"
        >
          {filtered.length === 0 && (
            <div className="px-3 py-2 text-[13px] text-muted italic">
              {i18nT('components.searchableSelect.no_matches')}
            </div>
          )}
          {filtered.map(opt => {
            const isSel = opt.value === value
            return (
              <button
                key={opt.value}
                type="button"
                role="option"
                aria-selected={isSel}
                tabIndex={-1}
                // `aria-disabled`, NOT the `disabled` attribute: a disabled button
                // cannot take focus, so `useListboxKeyboard`'s `.focus()` is a
                // no-op on it and ArrowDown stalls in the filter box. Steering's
                // first scope row is disabled when no project is configured, which
                // would strand the keyboard before "Global". `choose()` still
                // refuses the row, and `pointer-events-none` still refuses clicks.
                aria-disabled={opt.disabled || undefined}
                onClick={() => choose(opt)}
                className={[
                  'relative flex w-full cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] text-left outline-none transition-colors',
                  'focus:bg-bg-hover hover:bg-bg-hover aria-disabled:pointer-events-none aria-disabled:opacity-50',
                  isSel ? 'bg-accent-subtle text-accent font-semibold hover:bg-accent-subtle' : '',
                ].join(' ')}
              >
                {/* Label and sublabel stay ADJACENT rather than being pushed to
                    opposite edges. The popup is the trigger's width, and some
                    triggers are full-panel wide (the Schedule timezone picker is
                    ~845px) — `justify-between` there stranded "UTC-7" hundreds of
                    pixels from its zone name, so the eye had to track across
                    empty space to pair the two. The check indicator keeps its
                    right edge via `ml-auto`. */}
                <span className="truncate min-w-0">{opt.label}</span>
                {opt.sublabel && (
                  <span className={`shrink-0 text-[11px] ${isSel ? 'text-accent/70' : 'text-muted'}`}>
                    {opt.sublabel}
                  </span>
                )}
                {isSel && <Check size={13} className="text-accent shrink-0 ml-auto" aria-hidden />}
              </button>
            )
          })}
        </div>
      </PopoverContent>
    </Popover>
  )
}
