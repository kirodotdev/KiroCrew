import { useCallback, useRef } from 'react'
import type React from 'react'

/** Native click events whose press was already acted on at pointer-down. */
const consumedClicks = new WeakSet<Event>()

/** True for the click that closes a press `usePressActivation` already
 *  handled. A click-based outside-dismiss listener skips it: the press it
 *  belongs to opened (or toggled) a control on the way down, so treating the
 *  release as a click outside would undo what the press just did. */
export function isConsumedPressClick(e: Event): boolean {
  return consumedClicks.has(e)
}

/** A plain left-button mouse press. Touch and pen stay on click, so a finger
 *  that lands on a control to start a scroll never activates it; a modified
 *  press (ctrl on macOS opens the context menu) also falls through to click. */
export function isPlainMousePress(e: Pick<PointerEvent, 'pointerType' | 'button' | 'ctrlKey' | 'metaKey' | 'altKey' | 'shiftKey'>): boolean {
  return e.pointerType === 'mouse' && e.button === 0
    && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey
}

/** Activate a control on the mouse PRESS instead of the release.
 *
 *  For controls whose action only opens, toggles or selects something the
 *  user can take back with the next press (a picker chip, a tab), acting on
 *  pointer-down removes the press-to-release delay from every use. Radix
 *  already does this for DropdownMenu, Select and Tabs triggers.
 *
 *  Returns a binder: `bind(onPress)` gives `onPointerDown` + `onClick` for one
 *  element. A plain mouse press runs `onPress` at pointer-down and the click
 *  that follows is swallowed. Everything else (keyboard Enter/Space, touch,
 *  pen, a modified press, a programmatic `click()`) runs it from `onClick`
 *  exactly as before. One binder serves a whole list of controls, since only
 *  one press is ever in flight. */
export function usePressActivation() {
  const pressed = useRef<EventTarget | null>(null)
  return useCallback(<E extends HTMLElement>(onPress: (el: E) => void) => ({
    onPointerDown(e: React.PointerEvent<E>) {
      const el = e.currentTarget
      if (!isPlainMousePress(e) || el.matches(':disabled')) {
        pressed.current = null
        return
      }
      pressed.current = el
      onPress(el)
    },
    onClick(e: React.MouseEvent<E>) {
      // detail 0 is a keyboard or programmatic click: no press preceded it.
      const handledOnDown = pressed.current === e.currentTarget && e.detail > 0
      pressed.current = null
      if (handledOnDown) {
        consumedClicks.add(e.nativeEvent)
        return
      }
      onPress(e.currentTarget)
    },
  }), [])
}
