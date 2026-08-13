import { useCallback, useEffect } from 'react'
import { isTouchDevice } from '../utils/isTouchDevice'

/**
 * Roving-focus keyboard navigation for a portal listbox that pairs with
 * `useFilteredDropdown` (AgentSelector). Real DOM focus moves
 * across elements marked `data-option` inside `dropdownRef`; the trigger
 * regains focus on close (the caller owns `closeToTrigger`). This is the
 * WAI-ARIA listbox / combobox pattern.
 *
 * Distinct from `useListKeyboardNav` (selected-index highlight + a
 * document-level listener), which powers the typeahead pickers
 * (FilePickerMenu, SkillPickerMenu) whose query input lives outside the menu.
 *
 * Wiring in the consumer:
 *  - put `onKeyDown={onListKeyDown}` on the `role="listbox"` container
 *  - mark each focusable option with `data-option` or `role="option"`, plus `tabIndex={-1}`
 *  - render the filter input with `ref={inputRef}` (when present)
 */
export function useListboxKeyboard(opts: {
  open: boolean
  dropdownRef: React.RefObject<HTMLElement | null>
  inputRef: React.RefObject<HTMLElement | null>
  /** Whether a filter input is rendered + auto-focused inside the menu. */
  hasFilterInput: boolean
  /** Count of currently-visible options (for Enter-selects-sole-match). */
  filteredCount: number
  /** Select the sole remaining match (Enter pressed in the filter input). */
  onEnterSingleMatch: () => void
  /** Close the menu and move focus back to the trigger. */
  closeToTrigger: () => void
}) {
  const { open, dropdownRef, inputRef, hasFilterInput, filteredCount, onEnterSingleMatch, closeToTrigger } = opts

  const optionEls = useCallback(
    // AgentSelector marks options (and action rows) with `data-option`;
    // AgentSelector's option rows carry role="option". Match either.
    () => Array.from(dropdownRef.current?.querySelectorAll<HTMLElement>('[data-option],[role="option"]') ?? []),
    [dropdownRef],
  )

  // When no filter input is shown, move focus into the list on open so keyboard
  // users can reach the options. (When an input is shown, useFilteredDropdown
  // auto-focuses it.) Skip on touch to avoid hijacking focus.
  useEffect(() => {
    if (!open || hasFilterInput || isTouchDevice()) return
    const t = window.setTimeout(() => {
      const els = optionEls()
      const selected = els.find(el => el.getAttribute('aria-selected') === 'true')
      ;(selected ?? els[0])?.focus()
    }, 0)
    return () => clearTimeout(t)
  }, [open, hasFilterInput, optionEls])

  const onListKeyDown = useCallback((e: React.KeyboardEvent) => {
    const inInput = document.activeElement === inputRef.current
    switch (e.key) {
      case 'Escape':
      case 'Tab':
        // Consume so a nested menu doesn't also close a surrounding modal.
        e.preventDefault()
        e.stopPropagation()
        closeToTrigger()
        break
      case 'ArrowDown': {
        e.preventDefault()
        e.stopPropagation()
        const els = optionEls()
        const i = els.indexOf(document.activeElement as HTMLElement)
        // From the filter input (i === -1) this lands on the first option.
        ;(els[i + 1] ?? els[els.length - 1])?.focus()
        break
      }
      case 'ArrowUp': {
        e.preventDefault()
        e.stopPropagation()
        const els = optionEls()
        const i = els.indexOf(document.activeElement as HTMLElement)
        // From the first option, go back up to the filter input if present.
        if (i <= 0) (inputRef.current ?? els[0])?.focus()
        else els[i - 1]?.focus()
        break
      }
      case 'Home':
        if (inInput) break // let the caret move within the filter text
        e.preventDefault()
        e.stopPropagation()
        optionEls()[0]?.focus()
        break
      case 'End': {
        if (inInput) break
        e.preventDefault()
        e.stopPropagation()
        const els = optionEls()
        els[els.length - 1]?.focus()
        break
      }
      case 'Enter':
        // Enter on a focused option is handled natively (button click). Here we
        // only cover the filter input when it has narrowed to a single match.
        if (inInput && filteredCount === 1) {
          e.preventDefault()
          e.stopPropagation()
          onEnterSingleMatch()
        }
        break
    }
  }, [optionEls, inputRef, filteredCount, onEnterSingleMatch, closeToTrigger])

  return { onListKeyDown }
}
