import { useState, useEffect, useRef } from 'react'
import { isTouchDevice } from '../utils/isTouchDevice'
import { isConsumedPressClick, isPlainMousePress } from './usePressActivation'

/** Shared hook for filtered dropdown behavior (open/close, filter, click-outside, keyboard). */
export function useFilteredDropdown<T extends { name: string; display_name?: string }>(items: T[]) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) { setFilter(''); return }
    const close = (e: MouseEvent) => {
      if (dropdownRef.current?.contains(e.target as Node)) return
      // A help tooltip opened from inside the dropdown (InfoTip) is portaled
      // to document.body, so a click on its text lands outside dropdownRef.
      // That click is aimed at the help, not at dismissing the picker.
      if ((e.target as Element | null)?.closest?.('[role="tooltip"]')) return
      setOpen(false)
    }
    // A plain mouse press dismisses on the press, matching triggers that open
    // on the press (usePressActivation): pressing another picker's chip closes
    // this one in the same instant the other opens. Every other press (touch,
    // a right/middle button, a modified click) keeps dismissing on click, the
    // same split the triggers use, so a scroll that starts outside the list
    // does not close it and a shift-click on a chip does not close-then-reopen.
    const closeOnMousePress = (e: PointerEvent) => {
      if (isPlainMousePress(e)) close(e)
    }
    // The release of a press-activated trigger is not a click outside: the
    // press already acted, and here it may be the press that opened this list.
    const closeOnClick = (e: MouseEvent) => {
      if (!isConsumedPressClick(e)) close(e)
    }
    const t1 = setTimeout(() => {
      document.addEventListener('pointerdown', closeOnMousePress)
      document.addEventListener('click', closeOnClick)
    }, 0)
    // Skip auto-focus on touch — focusing pops the keyboard, which on iOS
    // Safari fires `window.resize` and can close the dropdown.
    const t2 = isTouchDevice()
      ? null
      : setTimeout(() => inputRef.current?.focus(), 0)
    return () => {
      clearTimeout(t1)
      if (t2 !== null) clearTimeout(t2)
      document.removeEventListener('pointerdown', closeOnMousePress)
      document.removeEventListener('click', closeOnClick)
    }
  }, [open])

  const filtered = filter
    ? items.filter(item => [item.name, item.display_name ?? ''].some(text => text.toLowerCase().includes(filter.toLowerCase())))
    : items

  return { open, setOpen, filter, setFilter, dropdownRef, inputRef, filtered }
}
