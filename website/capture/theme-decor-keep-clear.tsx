/**
 * Evidence: theme decoration never covers the chat composer.
 *
 * Mounts the REAL composer (through SideChat, as sidechat-composer.tsx does)
 * filling the page, plus the shell's decor slot with a corner mascot pinned the
 * way a registered theme pins one (fixed to the bottom-right corner, z-index 40; `?right=` moves it onto the composer).
 * The core ghost art stands in for the theme's character. `?fix=0` leaves the
 * slot unclipped, as on main; the default runs `useThemeDecorKeepClear`.
 * Component render, not the live page.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'

import SideChat from '../src/pages/chat/SideChat'
import dashboardReducer from '../src/store/dashboardSlice'
import { createTestStore } from '../src/test/helpers'
import reducer from '../src/store/chatSlice'
import { initI18n } from '../src/i18n/all'
import { GhostVar1 } from '../src/assets/onboarding/GhostIcons'
import { OVERLAY_Z_MAX } from '../src/lib/themeDecorLayer'
import { useThemeDecorKeepClear } from '../src/lib/themeDecorKeepClear'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const fixed = params.get('fix') !== '0'
document.documentElement.dataset.mode = 'dark'
document.documentElement.dataset.theme = 'kiro-dark'
initI18n()

const SLOT = 'capture-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const store = createTestStore({
  dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
  chat: {
    ...initial,
    activeSlot: SLOT,
    slotSide: {
      [SLOT]: {
        messages: [
          { role: 'user' as const, content: 'Why does the end of every line I type disappear?', ts: '2026-11-19T00:00:00Z', run_id: 'r1' },
          { role: 'assistant' as const, content: 'The theme character sits on the right edge of the composer. With the fix it tucks behind the composer instead of covering it.', ts: '2026-11-19T00:00:01Z', run_id: 'r1' },
        ],
        lastRunId: 'r1',
        pending: false,
        streaming: false,
        openedAtTurnCount: 0,
        createdAt: '2026-11-19T00:00:00Z',
      },
    },
  },
})

function DecorSlot() {
  const [slot, setSlot] = useState<HTMLDivElement | null>(null)
  useThemeDecorKeepClear(fixed ? slot : null)
  return (
    <div ref={setSlot} className="fixed inset-0 pointer-events-none" style={{ zIndex: OVERLAY_Z_MAX }}>
      <div data-capture-mascot style={{ position: 'fixed', bottom: -40, right: Number(params.get('right') ?? 40), zIndex: 40, pointerEvents: 'none' }}>
        <GhostVar1 width={170} />
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div data-capture-root style={{ position: 'fixed', inset: 0, background: 'var(--bg)', display: 'flex', flexDirection: 'column' }}>
        <SideChat slot={SLOT} />
      </div>
      <DecorSlot />
    </QueryClientProvider>
  </Provider>,
)
