import { useState, useEffect, useRef } from 'react'
import { isTouchDevice } from '../utils/isTouchDevice'

/** Shared hook for filtered dropdown behavior (open/close, filter, click-outside, keyboard). */
export function useFilteredDropdown<T extends { name: string }>(
  items: T[],
  /** Extra text the filter matches besides `name` — e.g. a model's display name
   *  so typing "Opus" finds `claude-opus-4.8`. */
  extraText?: (item: T) => string,
) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) { setFilter(''); return }
    const close = (e: MouseEvent) => {
      const content = dropdownRef.current
      if (content) {
        // `event.target` can be detached before this document listener runs: a
        // menu action may synchronously remove its own button (the effort
        // "use configured default" link does exactly that). `contains(target)`
        // then becomes false even though the click originated inside, closing
        // the picker. composedPath() snapshots the original propagation path
        // and remains authoritative after React commits that state update.
        const path = typeof e.composedPath === 'function' ? e.composedPath() : []
        if (content.contains(e.target as Node) || path.includes(content)) return
      }
      setOpen(false)
    }
    const t1 = setTimeout(() => document.addEventListener('click', close), 0)
    // Skip auto-focus on touch — focusing pops the keyboard, which on iOS
    // Safari fires `window.resize` and can close the dropdown.
    const t2 = isTouchDevice()
      ? null
      : setTimeout(() => inputRef.current?.focus(), 0)
    return () => {
      clearTimeout(t1)
      if (t2 !== null) clearTimeout(t2)
      document.removeEventListener('click', close)
    }
  }, [open])

  const needle = filter.toLowerCase()
  const filtered = filter
    ? items.filter(item =>
        item.name.toLowerCase().includes(needle)
        || (extraText ? extraText(item).toLowerCase().includes(needle) : false))
    : items

  return { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered }
}
