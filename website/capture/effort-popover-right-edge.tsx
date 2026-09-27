/**
 * Isolated capture entry for the reasoning-effort popover's right-edge clamp.
 *
 * WHY ISOLATED: the popover lives in the chat composer of an authenticated
 * dashboard, and what is under test is pure geometry — where its hosts place a
 * 240px portaled box when the chip that opens it sits near the right edge. This
 * frame renders the REAL `ReasoningEffortDropdown` inside a viewport-sized clip
 * box, positioned by the same expression the host uses, so a screenshot shows
 * exactly what a user at that window width sees.
 *
 * `fix=off` reproduces the pre-fix ChatPage expression (a hand-written
 * `innerWidth - 220` against a 240px popover); `fix=on` uses
 * `effortPopoverLeft`. `window.__measure()` reports the popover's right edge
 * against the frame, which is what the capture script asserts — a before frame
 * identical to the after frame is what a toggle that silently failed to apply
 * would produce.
 *
 * Query string: ?theme=dark|light&w=<frame width>&fix=on|off
 */
import { createRoot } from 'react-dom/client'
import { useLayoutEffect, useRef, useState } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import ReasoningEffortDropdown from '../src/components/ReasoningEffortDropdown'
import { effortPopoverLeft } from '../src/lib/effort'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
// Every label in the popover is a catalog string, so an uninitialised i18n
// would render an empty box and the frame would document nothing.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const frameWidth = Number(params.get('w') || 900)
const fixed = params.get('fix') !== 'off'
document.documentElement.setAttribute('data-theme', theme)

/** The pre-fix ChatPage expression, kept here only so the before frame is a
 *  reproduction rather than an assumption. */
function legacyLeft(anchorLeft: number, viewportWidth: number): number {
  return Math.max(8, Math.min(anchorLeft, viewportWidth - 220))
}

/** Where the composer chip sits: far enough right that the clamp decides the
 *  position, as in the bug report. */
const CHIP_RIGHT_INSET = 24
const CHIP_WIDTH = 120

declare global {
  interface Window {
    __measure: () => {
      fix: string
      frameWidth: number
      left: number
      right: number
      frameRight: number
      overflows: boolean
    }
  }
}

function Frame() {
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  const [store] = useState(() => configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  }))
  const popoverRef = useRef<HTMLDivElement>(null)
  const chipLeft = frameWidth - CHIP_RIGHT_INSET - CHIP_WIDTH
  const left = fixed ? effortPopoverLeft(chipLeft, frameWidth) : legacyLeft(chipLeft, frameWidth)

  useLayoutEffect(() => {
    window.__measure = () => {
      const el = popoverRef.current
      const frame = el?.parentElement
      const box = el?.getBoundingClientRect()
      const frameBox = frame?.getBoundingClientRect()
      // Measured against the FRAME, which stands in for the window: the frame
      // is what clips, so its right edge is the one the popover must stay
      // inside whatever the page's own margins are.
      const rel = (x: number) => Math.round(x - (frameBox?.left ?? 0))
      const right = rel(box?.right ?? 0)
      return {
        fix: fixed ? 'on' : 'off',
        frameWidth,
        left: rel(box?.left ?? 0),
        right,
        frameRight: frameWidth,
        // The popover's own gutter: a right edge past `frameWidth - 8` is off
        // screen or touching the edge, which is the reported defect.
        overflows: right > frameWidth - 8,
      }
    }
  }, [])

  return (
    <div
      data-scene="effort-popover"
      style={{
        position: 'relative',
        width: frameWidth,
        height: 420,
        overflow: 'hidden',
        // `outline`, not `border`: a border would inset the absolutely
        // positioned children by its own width and shift every measurement.
        outline: '1px solid var(--border)',
        background: 'var(--bg)',
      }}
    >
      {/* The composer chip the popover is anchored to. */}
      <div
        style={{
          position: 'absolute',
          left: chipLeft,
          bottom: 16,
          width: CHIP_WIDTH,
          height: 32,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: 9999,
          fontSize: 12,
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          color: 'var(--text)',
        }}
      >
        Effort: Max
      </div>
      {/* No width here: the hosts set only `left`/`bottom` on the portal and let
          the popover size itself, which is what makes its own
          `max-w-[calc(100vw-16px)]` shrink observable. */}
      <div ref={popoverRef} style={{ position: 'absolute', left, bottom: 56 }}>
        <QueryClientProvider client={queryClient}>
          <Provider store={store}>
            <ReasoningEffortDropdown
              slot="capture"
              currentEffort="max"
              defaultEffort="high"
              levelsOverride={['low', 'medium', 'high', 'xhigh', 'max']}
              onClose={() => {}}
            />
          </Provider>
        </QueryClientProvider>
      </div>
    </div>
  )
}

initI18n('en')
document.body.style.margin = '0'
createRoot(document.getElementById('root')!).render(<Frame />)
