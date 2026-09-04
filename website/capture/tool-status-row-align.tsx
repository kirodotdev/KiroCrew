/**
 * Visual evidence for "a tool row's status line starts on the pill label's own
 * left edge".
 *
 * THE BUG: the shell-activity line ("Running · 15s") and the `wait` countdown
 * carried `ml-3` (12px). The pill's label text sits at icon (12px) + the pill
 * button's gap-2 (8px) = 20px, so the status line landed 8px LEFT of the text
 * it continues — aligned with nothing (not the icon column at 0, not the
 * label at 20px).
 *
 * WHY ISOLATED: both lines exist only while a tool is mid-flight (a live shell
 * without output, a sleeping `wait`), which cannot be held still in a live
 * session long enough to photograph.
 *
 * WHAT IS FAITHFUL: the `after` scene renders the REAL ToolCallLine against a
 * preloaded store shaped exactly like the running-shell / sleeping-wait states
 * (same shape ToolCallLine.test.tsx pins), inside the literal px-4 row wrapper
 * every transcript row gets. The `before` scene renders the same components
 * with a scoped CSS override that restores the pre-fix `ml-3` on the two
 * status-line divs — markup otherwise identical, so the 4px delta is the only
 * difference between the frames.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6816 --strictPort
 *   node scripts/capture-tool-status-row-align.mjs http://127.0.0.1:6816 \
 *     ../temp-screenshots/tool-status-row-align
 */
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useLayoutEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import ToolCallLine from '../src/pages/chat/ToolCallLine'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import instancesReducer from '../src/store/instancesSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import type { RootState } from '../src/store'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const COLUMN = { maxWidth: 'var(--mc-content-width, 900px)' } as const

/** Dashed guide on the pill label's text edge, positioned by measuring the
 *  rendered label node after layout. Re-measured on resize only — the capture
 *  viewport is static. */
function MeasuredGuide() {
  const [x, setX] = useState<number | null>(null)
  useLayoutEffect(() => {
    const measure = () => {
      const label = document.querySelector('[data-row="shell tool"] button span:not(.sr-only)')
      const root = document.querySelector('[data-capture-root]')
      if (!label || !root) return
      setX(label.getBoundingClientRect().x - root.getBoundingClientRect().x)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])
  if (x == null) return null
  return (
    <div
      className="absolute top-0 bottom-0 border-l border-dashed border-accent/40 pointer-events-none"
      style={{ left: x }}
    />
  )
}

const SHELL_MSG: ChatMessage = {
  role: 'tool',
  content: '🔧 Running: python -m pytest -q',
  cls: '',
  meta: { tool_call_id: 'tc_shell' },
}
const WAIT_MSG: ChatMessage = {
  role: 'tool',
  content: '🔧 wait',
  cls: '',
  meta: { tool_call_id: 'tc_wait' },
}

/** Store shaped like a turn with a live shell command and a sleeping wait —
 *  the same state shape ToolCallLine.test.tsx builds for these rows. */
const store = configureStore({
  reducer: {
    dashboard: dashboardReducer,
    chat: chatReducer,
    notifications: notificationsReducer,
    instances: instancesReducer,
  },
  preloadedState: {
    chat: {
      messages: [SHELL_MSG, WAIT_MSG],
      toolLog: [
        { type: 'tool', text: 'python -m pytest -q', tool_call_id: 'tc_shell', is_shell: true, ts: Date.now() - 75_000 },
        { type: 'tool', text: 'wait', tool_call_id: 'tc_wait', ts: Date.now() - 10_000 },
      ],
      slotRunning: true,
      activeSlot: 'cap',
    },
    dashboard: {
      slots: [{ key: 'cap', running: true, wait_state: { deadline_ts: Math.floor(Date.now() / 1000) + 290 } }],
    },
  } as unknown as Partial<RootState>,
})

initI18n('en')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
  <Provider store={store}>
    <MemoryRouter>
      <div
        data-capture-root
        data-scene={scene}
        className="bg-bg text-text relative"
        style={{ width: 900, ['--mc-content-width' as string]: '800px' }}
      >
        {scene === 'before' && (
          // The pre-fix geometry, restored via a scoped override so the frames
          // differ by nothing except the margin under test.
          <style>{`
            [data-scene="before"] [data-testid="wait-countdown"],
            [data-scene="before"] [data-testid="shell-activity"] { margin-left: 0.75rem !important; }
          `}</style>
        )}
        {/* The pill label's text edge, MEASURED off the rendered label itself
            (never hand-computed from padding arithmetic — a constant derived
            the same way as the fix would self-confirm a mis-measured edge). */}
        <MeasuredGuide />
        <div className="py-4">
          <div data-row="shell tool" className="px-4 mx-auto w-full py-1" style={COLUMN}>
            <ToolCallLine message={SHELL_MSG} running />
          </div>
          <div data-row="wait tool" className="px-4 mx-auto w-full py-1" style={COLUMN}>
            <ToolCallLine message={WAIT_MSG} running />
          </div>
        </div>
      </div>
    </MemoryRouter>
  </Provider>
  </QueryClientProvider>,
)
