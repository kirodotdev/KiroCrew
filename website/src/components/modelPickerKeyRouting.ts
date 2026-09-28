import type { KeyboardEvent } from 'react'

/**
 * Keyboard routing for a model picker dialog that embeds the reasoning-effort
 * controls below its searchable option list (one control, see
 * docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md).
 *
 * `useListboxKeyboard` owns the list: it closes the menu on Tab and moves
 * focus across options on the arrows. With controls embedded below the list,
 * keys have to route INTO them first and stay native once inside them:
 *
 * - Tab from the filter input, the manage row or the effort help button ->
 *   the next focusable stop AFTER it in DOM order, whatever it is: a Retry
 *   button a failed models read rendered above the list counts, so it is
 *   not skipped in favour of the embedded controls below it (the list's
 *   options carry `tabIndex={-1}` and are never stops). The Tab is claimed
 *   through the IME latch BEFORE any focus move: an IME uses Tab to cycle its
 *   candidates, and a committing Tab must not yank focus
 *   (`ImeEnterClaimRatchet`). Any other Tab stays native
 *   while it lands on another stop inside the dialog; a Tab whose native
 *   destination is OUTSIDE the dialog is the listbox's own dismissal and goes
 *   to `onListKeyDown`, which closes to the trigger. Left native, that Tab
 *   would move focus out of the portal with the dialog still open behind it.
 * - ArrowDown from the last option -> first ARROW stop; from the manage row
 *   or the help button -> the next one after it. ArrowUp from the manage row,
 *   the help button or the switch -> the previous arrow stop, or the last
 *   option once there is none.
 * - Inside the slider / switch / manage row / help button every other key
 *   stays native, EXCEPT Escape, which still dismisses the picker through
 *   `onListKeyDown`.
 * - Everything else -> `onListKeyDown`.
 *
 * The embedded controls, in DOM order: the first-use "Manage visible models"
 * row, then the effort block's "More information" help button (its wrapper
 * marked `data-model-picker-stop`), the reasoning-effort slider and its "Use default
 * effort" switch. The help button and the slider are TAB stops only, never
 * arrow stops. The help button is skipped by arrow hops because it is
 * secondary to the controls the hop is reaching for; the slider because it
 * answers ArrowUp / ArrowDown itself (one level up or down, persisted to the
 * slot), so an arrow hop that parked focus on it would turn the very next
 * list-navigation keystroke into a silent effort write. Arrow hops land on
 * the switch instead.
 * A disabled slider (the slot runs at the default effort) is skipped by Tab
 * too: it has `tabIndex={-1}` and answers no key, so parking focus on it would
 * strand the user on an inert control. The switch that enables it is the next
 * stop.
 *
 * Both pickers (`ModelEffortDropdown` and the split pane's inline dialog in
 * `ChatPane`) call this so the two cannot drift apart.
 */
export function routeModelPickerKeys(
  event: KeyboardEvent,
  claimKey: (e: KeyboardEvent) => boolean,
  onListKeyDown: (e: KeyboardEvent) => void,
): void {
  const target = event.target as HTMLElement
  const dialog = event.currentTarget as HTMLElement
  const enabled = (control: HTMLElement) => control.getAttribute('aria-disabled') !== 'true'
  // Arrow stops: the manage row and the switch only -- never the help button
  // or the slider (see the module comment). Tab stops are `nativeStops` below.
  const arrowStops = () =>
    Array.from(dialog.querySelectorAll<HTMLElement>('[data-model-picker-manage], [role="switch"]')).filter(enabled)
  // The nearest arrow stop past `control` in DOM order; `control` itself need
  // not be an arrow stop (the help button is a Tab stop only).
  const arrowStopFrom = (control: HTMLElement, delta: 1 | -1) => {
    const direction = delta === 1 ? Node.DOCUMENT_POSITION_FOLLOWING : Node.DOCUMENT_POSITION_PRECEDING
    const hits = arrowStops().filter(el => el !== control && (control.compareDocumentPosition(el) & direction) !== 0)
    return delta === 1 ? hits[0] : hits[hits.length - 1]
  }
  const lastOption = () => {
    const options = dialog.querySelectorAll<HTMLElement>('[role="option"]')
    return options[options.length - 1]
  }
  const moveTo = (control: HTMLElement | undefined) => {
    if (!control) return false
    event.preventDefault()
    event.stopPropagation()
    control.focus()
    return true
  }
  // The native Tab stops inside the dialog in the key's direction from
  // `target`: any focusable element (a Retry row above the list, the footer
  // rows, the switch after the slider) before or after it in DOM order.
  // Options carry `tabIndex={-1}`, so they are not stops.
  const nativeStops = () => {
    const direction = event.shiftKey ? Node.DOCUMENT_POSITION_PRECEDING : Node.DOCUMENT_POSITION_FOLLOWING
    return Array.from(dialog.querySelectorAll<HTMLElement>('input, button, [tabindex]'))
      .filter(el => el !== target && el.tabIndex >= 0 && !el.matches(':disabled') && el.getAttribute('aria-disabled') !== 'true')
      .filter(el => (target.compareDocumentPosition(el) & direction) !== 0)
  }
  const nativeTabStaysInside = () => nativeStops().length > 0
  // A row-like stop: the manage row or the help button. Keys inside it stay
  // native apart from the arrow hops and Escape.
  const rowStop = target.closest<HTMLElement>('[data-model-picker-manage],[data-model-picker-stop]')

  if (event.key === 'Tab') {
    if (!claimKey(event)) return
    let moved = false
    // Forward Tab from the filter or a row stop is routed (not left native)
    // only because the listbox would otherwise close on it; the destination
    // is still what native Tab would reach -- the nearest stop after it.
    if (!event.shiftKey && (target.tagName === 'INPUT' || rowStop)) moved = moveTo(nativeStops()[0])
    if (!moved && !nativeTabStaysInside()) onListKeyDown(event)
    return
  }
  const effortControl = target.closest<HTMLElement>('[role="slider"],[role="switch"]')
  if (effortControl) {
    if (event.key === 'Escape') onListKeyDown(event)
    // The switch is the arrow hop's landing point, so ArrowUp from it must
    // lead back out; the slider keeps ArrowUp native (one level up).
    else if (event.key === 'ArrowUp' && effortControl.getAttribute('role') === 'switch') {
      moveTo(arrowStopFrom(effortControl, -1) ?? lastOption())
    }
    return
  }
  if (rowStop) {
    if (event.key === 'ArrowDown') moveTo(arrowStopFrom(rowStop, 1))
    else if (event.key === 'ArrowUp') moveTo(arrowStopFrom(rowStop, -1) ?? lastOption())
    else if (event.key === 'Escape') onListKeyDown(event)
    return
  }
  if (event.key === 'ArrowDown' && target.getAttribute('role') === 'option') {
    if (target === lastOption() && moveTo(arrowStops()[0])) return
  }
  onListKeyDown(event)
}
