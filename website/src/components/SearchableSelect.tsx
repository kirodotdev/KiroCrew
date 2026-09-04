import { useCallback, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { Popover, PopoverTrigger, PopoverContent } from './ui/popover'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useImeGuard } from '../hooks/useImeGuard'

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
  /**
   * CSS font stack to render this row's own text in, for a list whose rows ARE a
   * sample of what they select — a font picker showing each family in that
   * family, so the choice is visible before it is made. A font stack rather than
   * a style object on purpose: the narrow type is the promise, so the row can
   * never become a general styling hook.
   */
  previewFontFamily?: string
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
  /**
   * Let the filter box double as free text: PASSING this shapes the row that
   * offers whatever is typed, and its presence is what enables free text at all.
   *
   * For a list that can only ever be a SAMPLE of the valid values — installed
   * fonts, where the browser can only confirm names it is handed — rather than a
   * closed enumeration like the IANA timezones. It returns everything but the
   * `value`, so the row can carry a preview and so the copy stays in the caller's
   * catalog; there is no default label, because a bare echo of the typed text
   * reads as an option that already exists.
   */
  customValueOption?: (typed: string) => Omit<SearchableSelectOption, 'value'>
  /**
   * Optional action row at the top of the list (e.g. "List all installed
   * fonts…"). Fires `onSelect` instead of `onChange` and does not close the
   * popup, so a slow action can repopulate `options` in place. Mirrors
   * `SimpleSelect`'s `action`.
   */
  action?: { label: string; onSelect: () => void }
  /**
   * One-line outcome of the last `action` run, rendered inside the popup next to
   * it. The popup overlays the settings row below the trigger, so an action's
   * result reported there would be hidden behind the very list it describes.
   */
  actionStatus?: string
  disabled?: boolean
  /** Set on the trigger so an external <label htmlFor> can address it. */
  id?: string
  className?: string
  style?: React.CSSProperties
  'aria-label'?: string
}

/** Row text style, from the option's font stack — the only styling a row exposes. */
function preview(opt: SearchableSelectOption): React.CSSProperties | undefined {
  return opt.previewFontFamily ? { fontFamily: opt.previewFontFamily } : undefined
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
  customValueOption,
  action,
  actionStatus,
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
  const ime = useImeGuard()

  const selected = options.find(o => o.value === value)

  const filtered = useMemo(() => {
    const typed = filter.trim()
    const tokens = typed.toLowerCase().split(/\s+/).filter(Boolean)
    const rows = options.filter(o => matches(o, tokens))
    // The free-text row goes LAST, so Enter still commits the top real match and
    // the typed value is offered rather than imposed. It is the only row when
    // nothing matched, which is exactly when the escape hatch is needed.
    if (customValueOption && typed
      && !rows.some(o => o.value.toLowerCase() === typed.toLowerCase())) {
      rows.push({ value: typed, ...customValueOption(typed) })
    }
    return rows
  }, [options, filter, customValueOption])

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
    // combobox convention commits the top match whenever the user has NARROWED
    // the list: typing "los ang" and pressing Enter should not sit silent just
    // because two rows still match. Reporting 1 whenever anything matches routes
    // Enter here, and `choose(filtered[0])` picks the row the user is looking at.
    //
    // Gated on a non-empty filter, because on an UNFILTERED list `filtered[0]` is
    // merely the first row, not a match for anything the user expressed. Opening
    // the picker and pressing Enter would otherwise commit it and overwrite the
    // saved value — with a "default" row at the top, that silently discards the
    // user's choice.
    filteredCount: filter.trim() && filtered.length > 0 ? 1 : 0,
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
            {...ime.bindComposition<HTMLInputElement>()}
            onKeyDown={e => {
              // An IME confirms a composition with Enter. Letting that through would
              // commit `filtered[0]` and close the picker instead of accepting the
              // composed text — so every CJK user typing into this box would lose their
              // input on the first Enter. The guard is the host's rather than the native
              // flag alone, because WebKit dispatches that committing keydown AFTER
              // `compositionend`, where the native flag already reads false.
              // `isComposing`, not `claimEnter`: this handler also carries the arrow keys,
              // and claiming those would suppress list navigation.
              if (ime.isComposing(e)) return
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
          {action && (
            // Inside the list, but a `button` rather than an `option`: it commits
            // no value. `data-option` enrols it in the roving-focus ring
            // (useListboxKeyboard matches `[data-option],[role="option"]`) so an
            // Arrow key reaches it — the hook's Tab branch closes the popup, so a
            // control placed outside the list is pointer-only. Same shape as
            // AgentDropdownList's set-default control.
            <div
              role="button"
              data-option
              tabIndex={-1}
              onClick={action.onSelect}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  e.stopPropagation()
                  action.onSelect()
                }
              }}
              className="flex w-full cursor-pointer select-none items-center rounded-md px-3 py-1.5 text-[13px]
                text-accent outline-none transition-colors hover:bg-bg-hover focus:bg-bg-hover"
            >
              {action.label}
            </div>
          )}
          {actionStatus && (
            <div className="px-3 py-1.5 text-[12px] text-muted" role="status">
              {actionStatus}
            </div>
          )}
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
                <span className="truncate min-w-0" style={preview(opt)}>{opt.label}</span>
                {opt.sublabel && (
                  <span
                    className={`shrink-0 text-[11px] ${isSel ? 'text-accent/70' : 'text-muted'}`}
                    style={preview(opt)}
                  >
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
