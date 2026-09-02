import { useState, useEffect, useRef } from 'react'
import { isTouchDevice } from '../utils/isTouchDevice'

/** Shared hook for filtered dropdown behavior (open/close, filter, click-outside, keyboard). */
export function useFilteredDropdown<T extends { name: string }>(items: T[]) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) { setFilter(''); return }
    const close = (e: MouseEvent) => {
      if (dropdownRef.current?.contains(e.target as Node)) return
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

  const filtered = filter
    ? items.filter(item => item.name.toLowerCase().includes(filter.toLowerCase()))
    : items

  return { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered }
}
