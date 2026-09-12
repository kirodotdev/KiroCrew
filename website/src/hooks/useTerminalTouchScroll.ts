import { useEffect, type RefObject } from 'react'
import type { Terminal } from '@xterm/xterm'
import { touchScrollAction, scrollSequence, touchCellFromRect } from '../state/touchScroll'
import { sendRawBytesToTerminalSession } from '../utils/terminalRegistry'

/** px of vertical drag per scroll step. */
const TOUCH_LINE_PX = 24

/**
 * One-finger touch swipe-to-scroll for the xterm terminal.
 *
 * xterm's built-in touch scroll pans its own scrollback, which works at a shell
 * prompt but does nothing for a full-screen app: an alt-screen program (claude,
 * vim, less) has no scrollback, and while it holds mouse tracking xterm refuses
 * to pan at all. Those apps scroll on scroll INPUT — a mouse wheel on desktop —
 * which a touch drag never produces. This translates the drag into that input:
 *
 *   - normal screen, no mouse tracking → left to xterm/browser (native pan)
 *   - alt screen, no mouse tracking     → arrow keys (pagers)
 *   - mouse tracking on (any screen)    → SGR wheel reports at the finger cell
 *
 * Listeners are attached natively (not via React props) so `preventDefault` on
 * `touchmove` is honored, and `touch-action: none` is toggled on the container
 * whenever we intend to intercept — otherwise the browser claims the vertical
 * pan first and delivers the subsequent moves as non-cancelable events.
 */
export function useTerminalTouchScroll(
  term: Terminal,
  sessionId: string,
  containerRef: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const mouseActive = () => term.modes.mouseTrackingMode !== 'none'
    const isAlt = () => term.buffer.active.type === 'alternate'

    // touch-action must be `none` BEFORE a gesture starts for us to be able to
    // swallow it, so track the intercept state on buffer/mode changes rather
    // than at touchstart (too late for the current gesture).
    const syncTouchAction = () => {
      container.classList.toggle('term-scroll-intercept', mouseActive() || isAlt())
    }
    syncTouchAction()
    const bufDisp = term.buffer.onBufferChange(syncTouchAction)

    let touchY: number | null = null
    let accum = 0

    const onStart = (e: TouchEvent) => {
      touchY = e.touches.length === 1 ? e.touches[0].clientY : null
      accum = 0
    }
    const onMove = (e: TouchEvent) => {
      if (touchY == null || e.touches.length !== 1) return
      const action = touchScrollAction(isAlt(), mouseActive())
      if (action === 'native') return // let the browser pan xterm's scrollback

      e.preventDefault() // we drive the app; don't also pan the page
      const y = e.touches[0].clientY
      accum += touchY - y // finger up (y decreases) = scroll DOWN in content
      touchY = y
      const steps = Math.trunc(accum / TOUCH_LINE_PX)
      if (steps === 0) return
      accum -= steps * TOUCH_LINE_PX // keep the remainder for smoothness

      const dir = steps > 0 ? 'down' : 'up'
      const appCursor = Boolean(term.modes.applicationCursorKeysMode)
      const screen = container.querySelector('.xterm-screen') ?? container
      const rect = screen.getBoundingClientRect()
      const [col, row] = touchCellFromRect(rect, term.cols, term.rows, e.touches[0].clientX, y)
      const bytes = scrollSequence(action, dir, Math.abs(steps), appCursor, col, row)
      sendRawBytesToTerminalSession(sessionId, bytes)
    }
    const onEnd = () => {
      touchY = null
      accum = 0
    }

    container.addEventListener('touchstart', onStart, { passive: true })
    container.addEventListener('touchmove', onMove, { passive: false })
    container.addEventListener('touchend', onEnd, { passive: true })
    container.addEventListener('touchcancel', onEnd, { passive: true })
    return () => {
      bufDisp.dispose()
      container.classList.remove('term-scroll-intercept')
      container.removeEventListener('touchstart', onStart)
      container.removeEventListener('touchmove', onMove)
      container.removeEventListener('touchend', onEnd)
      container.removeEventListener('touchcancel', onEnd)
    }
  }, [term, sessionId, containerRef])
}
