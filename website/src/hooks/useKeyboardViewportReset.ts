import { useEffect } from 'react'

/** Visual viewport is considered keyboard-reduced when it is this much shorter
 *  than the layout viewport. Large enough to ignore Safari's collapsing URL
 *  bar (~50-80px), small enough to catch the iPad floating/split keyboards'
 *  shortcut bar plus any docked keyboard. */
const KEYBOARD_THRESHOLD_PX = 120

/** iOS animates the keyboard away; the scroll offset is only final after it. */
const SETTLE_DELAYS_MS = [0, 120, 350, 700]

const SHELL_SELECTOR = '[data-testid="dashboard-shell"]'

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

    const keyboardOpen = () =>
      !!vv && window.innerHeight - vv.height > KEYBOARD_THRESHOLD_PX

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
