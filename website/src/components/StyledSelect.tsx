import { useRef, useCallback, useEffect } from 'react'
import { Check } from 'lucide-react'
import { createPortal } from 'react-dom'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { isTouchDevice } from '../utils/isTouchDevice'

import { i18nT } from '../i18n/t'
interface Props {
  options: string[]
  value: string
  onChange: (value: string) => void
  /** Optional action item shown at the top of the list (e.g. "+ New workspace…") */
  action?: { label: string; onSelect: () => void }
  /** Placeholder when no value is selected */
  placeholder?: string
  style?: React.CSSProperties
}

/** Portal-based styled dropdown matching AgentSelector's look. */
export default function StyledSelect({ options, value, onChange, action, placeholder, style }: Props) {
  const btnRef = useRef<HTMLButtonElement>(null)
  const items = options.map(o => ({ name: o }))
  const { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered } = useFilteredDropdown(items)

  /** Close the listbox and return focus to the trigger (WAI-ARIA combobox). */
  const closeToTrigger = useCallback(() => {
    setOpen(false)
    btnRef.current?.focus()
  }, [setOpen])

  const select = useCallback((v: string) => { onChange(v); closeToTrigger() }, [onChange, closeToTrigger])

  // Roving-focus keyboard navigation (shared with AgentSelector via useListboxKeyboard).
  const { onListKeyDown } = useListboxKeyboard({
    open,
    dropdownRef,
    inputRef,
    hasFilterInput: options.length > 5,
    filteredCount: filtered.length,
    onEnterSingleMatch: () => select(filtered[0].name),
    closeToTrigger,
  })

  // Close dropdown on scroll (outside the dropdown) or resize so it doesn't float detached
  useEffect(() => {
    if (!open) return
    const close = (e: Event) => {
      // Don't close if scrolling inside the dropdown itself
      if (e.target instanceof Node && dropdownRef.current?.contains(e.target)) return
      setOpen(false)
    }
    const closeResize = () => {
      // iOS Safari fires `window.resize` when the on-screen keyboard pops. If
      // focus is inside the dropdown on a touch device, treat the resize as the
      // keyboard, not a real layout change, and stay open.
      if (dropdownRef.current?.contains(document.activeElement) && isTouchDevice()) return
      setOpen(false)
    }
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', closeResize)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', closeResize)
    }
  }, [open, setOpen, dropdownRef])

  return (
    <div className="relative" style={style}>
      <button
        ref={btnRef}
        type="button"
        className="flex items-center justify-between w-full px-3 py-2 rounded-md text-sm font-mono border border-border bg-bg-elevated text-text hover:border-border-strong transition-all cursor-pointer"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="truncate">{value || placeholder || '—'}</span>
        <span className="text-muted text-[11px] ml-2 shrink-0">▾</span>
      </button>
      {open && btnRef.current && createPortal(
        // Presentational positioning wrapper: the interactive semantics live on
        // the inner role="listbox" and its option buttons. This element only
        // hosts the roving-focus keydown handler for the composite widget, so it
        // has no ARIA role of its own.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions
        <div
          ref={dropdownRef}
          tabIndex={-1}
          onKeyDown={onListKeyDown}
          className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[180px] max-h-[240px] flex flex-col animate-slide-up overflow-hidden"
          style={(() => {
            const r = btnRef.current!.getBoundingClientRect()
            const dropH = 240
            const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4
            const left = Math.max(8, Math.min(r.left, window.innerWidth - 200))
            const width = Math.max(r.width, 180)
            return { top, left, width }
          })()}
        >
          {options.length > 5 && (
            <div className="px-2 py-1.5 border-b border-border">
              <input
                ref={inputRef}
                type="text"
                aria-label={i18nT('components.styledSelect.filter_options')}
                placeholder={i18nT('components.styledSelect.filter')}
                value={filter}
                onChange={e => setFilter(e.target.value)}
                className="w-full px-2 py-1 rounded text-[13px] font-mono bg-bg-elevated border border-border text-text placeholder:text-muted focus:outline-none focus:border-accent"
              />
            </div>
          )}
          <div role="listbox" aria-label={placeholder || 'Options'} className="overflow-y-auto overflow-x-hidden rounded-b-lg">
            {action && (
              <button
                data-option
                tabIndex={-1}
                className="w-full text-left px-3 py-2 text-[13px] font-mono text-accent hover:bg-bg-hover cursor-pointer border-b border-border transition-colors focus:outline-none focus:ring-1 focus:ring-inset focus:ring-accent"
                onClick={() => { setOpen(false); action.onSelect() }}
              >
                {action.label}
              </button>
            )}
            {placeholder && (
              <button
                data-option
                tabIndex={-1}
                role="option"
                aria-selected={!value}
                className={`w-full text-left px-3 py-2 text-[13px] font-mono cursor-pointer border-b border-border transition-colors focus:outline-none focus:ring-1 focus:ring-inset focus:ring-accent ${!value ? 'bg-accent-subtle text-accent font-semibold' : 'text-muted hover:bg-bg-hover'}`}
                onClick={() => select('')}
              >
                {placeholder}
                {!value && <span className="float-right text-accent text-[11px]"><Check className="lucide-inline" /></span>}
              </button>
            )}
            {filtered.map(item => (
              <button
                key={item.name}
                data-option
                tabIndex={-1}
                role="option"
                aria-selected={item.name === value}
                className={`w-full text-left px-3 py-2 text-[13px] font-mono cursor-pointer border-b border-border last:border-0 transition-colors focus:outline-none focus:ring-1 focus:ring-inset focus:ring-accent ${item.name === value ? 'bg-accent-subtle text-accent font-semibold' : 'text-text hover:bg-bg-hover'}`}
                onClick={() => select(item.name)}
              >
                <span className="truncate">{item.name}</span>
                {item.name === value && <span className="float-right text-accent text-[11px]"><Check className="lucide-inline" /></span>}
              </button>
            ))}
            {filtered.length === 0 && <div className="px-3 py-2 text-[13px] text-muted italic">{i18nT('components.styledSelect.no_matches')}</div>}
          </div>
        </div>,
        document.body
      )}
    </div>
  )
}
