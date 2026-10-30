import { useEffect } from 'react'

/** Visual viewport is considered keyboard-reduced when it is this much shorter
 *  than the layout viewport. Large enough to ignore Safari's collapsing URL
 *  bar (~50-80px), small enough to catch the iPad floating/split keyboards'
 *  shortcut bar plus any docked keyboard. */
const KEYBOARD_THRESHOLD_PX = 120

/** iOS animates the keyboard away; the scroll offset is only final after it. */
const SETTLE_DELAYS_MS = [0, 120, 350, 700]

const SHELL_SELECTOR = '[data-testid="dashboard-shell"]'

/** Input types that never raise the on-screen keyboard. */
const NON_TEXT_INPUTS = new Set([
  'button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit',
])

/**
 * Keep the height-locked dashboard shell pinned to the top of the page after
 * the on-screen keyboard is dismissed.
 *
 * Why: iOS/iPadOS Safari pans the layout viewport to keep a focused input
 * visible. `overflow:hidden` on <body> does not stop that, and Safari does not
 * pan back when the keyboard hides, so the page header ends up above the fold
 * and a strip of empty space shows under the shell until the user scrolls by
 * hand. The shell never legitimately scrolls the document, so once the keyboard
 * is gone we put the scroll offset back to 0,0.
 *
 * Deliberately a no-op while the keyboard is open (the pan is what keeps the
 * composer above the keyboard) and on routes without the dashboard shell
 * (onboarding/welcome views keep their own document scroll).
 */
export function useKeyboardViewportReset(): void {
  useEffect(() => {
    if (typeof window === 'undefined') return
    const vv = window.visualViewport
    const timers = new Set<number>()

    // The keyboard-free height to compare against. iPadOS keeps innerHeight at
    // the full layout height, but iPhone Safari shrinks innerHeight together
    // with visualViewport.height, so innerHeight alone reads an open keyboard
    // as closed there. Also keep the tallest visual height seen at this width
    // (a rotation or window resize changes the width and starts over).
    // The width is the layout width (documentElement.clientWidth): pinch-zoom
    // shrinks visualViewport.width, and on iOS innerWidth with it, but leaves
    // the layout width alone, so zooming does not restart the record.
    const layoutWidth = () => document.documentElement.clientWidth
    // A restart while a text field has focus may happen with the keyboard up
    // (rotating mid-typing), so the record cannot tell an open keyboard from a
    // closed one. Treat it as open until that focus ends; by then the keyboard
    // is closing and the record has grown to the keyboard-free height.
    const textFieldFocused = () => {
      const el = document.activeElement
      if (!(el instanceof HTMLElement)) return false
      if (el.isContentEditable || el instanceof HTMLTextAreaElement) return true
      return el instanceof HTMLInputElement && !NON_TEXT_INPUTS.has(el.type)
    }
    let restWidth = layoutWidth()
    let restHeight = vv?.height ?? 0
    let restartedWhileTyping = false
    const keyboardOpen = () => {
      if (!vv) return false
      const width = layoutWidth()
      if (width !== restWidth) {
        restWidth = width
        restHeight = vv.height
        restartedWhileTyping = textFieldFocused()
      }
      restHeight = Math.max(restHeight, vv.height)
      if (restartedWhileTyping && !textFieldFocused()) restartedWhileTyping = false
      if (restartedWhileTyping) return true
      return Math.max(window.innerHeight, restHeight) - vv.height > KEYBOARD_THRESHOLD_PX
    }

    const reset = () => {
      if (keyboardOpen()) return
      if (!document.querySelector(SHELL_SELECTOR)) return
      const root = document.scrollingElement ?? document.documentElement
      if (window.scrollX !== 0 || window.scrollY !== 0 || root.scrollTop !== 0 || (vv && vv.offsetTop !== 0)) {
        window.scrollTo(0, 0)
      }
    }

    const scheduleReset = () => {
      for (const delay of SETTLE_DELAYS_MS) {
        const id = window.setTimeout(() => {
          timers.delete(id)
          reset()
        }, delay)
        timers.add(id)
      }
    }

    let wasOpen = keyboardOpen()
    const onViewportChange = () => {
      const open = keyboardOpen()
      // Only act on the open -> closed transition, or on any viewport movement
      // while closed (covers the late offsetTop correction iOS emits).
      if (wasOpen && !open) scheduleReset()
      else if (!open) reset()
      wasOpen = open
    }
    const onFocusOut = () => scheduleReset()
    // A plain window scroll is AMBIGUOUS: it also fires as iOS pans the layout
    // viewport to reveal a focused input, and that pan can arrive BEFORE
    // visualViewport reports the reduced height, so keyboardOpen() still reads
    // false. Resetting synchronously there would cancel the very pan that keeps
    // the composer above the keyboard. So defer through scheduleReset(), whose
    // settle delays re-check keyboardOpen() after the animation — by then the
    // viewport has shrunk and an open keyboard is a no-op, while a genuine
    // stale pan (keyboard already gone) is undone.
    const onWindowScroll = () => scheduleReset()

    vv?.addEventListener('resize', onViewportChange)
    vv?.addEventListener('scroll', onViewportChange)
    document.addEventListener('focusout', onFocusOut)
    // Chrome on iPad shows a ~90px autofill accessory bar (passwords / cards /
    // addresses) above the keyboard and leaves the page panned by that amount
    // when it hides, without always firing a visualViewport resize. The shell
    // never scrolls the document, so a window scroll that is still non-zero
    // once the keyboard is gone is the stale pan: undo it.
    window.addEventListener('scroll', onWindowScroll, { passive: true })
    return () => {
      vv?.removeEventListener('resize', onViewportChange)
      vv?.removeEventListener('scroll', onViewportChange)
      document.removeEventListener('focusout', onFocusOut)
      window.removeEventListener('scroll', onWindowScroll)
      timers.forEach((id) => window.clearTimeout(id))
      timers.clear()
    }
  }, [])
}
