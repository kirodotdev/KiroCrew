/**
 * Isolated capture entry for the HOVER HOLD on session row order.
 *
 * WHY ISOLATED: the delta needs two things to coincide that a live gateway
 * cannot be asked for on cue — a pointer resting on one row, and a DIFFERENT
 * session going active at that moment so the list re-sorts underneath it. On a
 * live dashboard the second half arrives whenever an agent happens to emit a
 * turn, which is not a photographable schedule.
 *
 * What MUST stay faithful is the two inputs the fix reads, and both do:
 *   - the hover is a REAL pointer hover, dispatched by Playwright, so the
 *     delegated `onPointerOver` on the sidebar root fires with
 *     `pointerType === 'mouse'` exactly as it does for a user. Nothing here
 *     stubs or simulates the hold.
 *   - the re-sort is a REAL slots update through the REAL `ChatSidebar`
 *     comparator; `bump()` only supplies newer `last_ts` values, the same shape
 *     the recency bump produces.
 *
 * So the harness supplies the occasion, and the component under test decides
 * the outcome.
 *
 * Query string: ?theme=dark|light
 * Window hook:  __hoverHoldBump()  — makes the two oldest sessions the newest,
 *               which under date-desc wants to lift them above the hovered row.
 */
import { useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
// ThemeProvider is the authority: it reads `mc-theme` / `mc-color-theme` and
// writes data-theme itself, so setting the attribute here is overridden on mount
// (which is how the first pass captured two light frames and one dark from the
// same ?theme=dark). Both keys are pinned because the colour theme alone drifted
// between runs — one pass rendered kiro-dark and the next monokai-dark.
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Rows must stay in the one list rather than folding into the stale section —
// the subject is their ORDER, so every row has to remain visible.
localStorage.setItem('mc-session-stale-collapse-ms', '0')

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

const slot = (key: string, title: string, lastTs: string, msg: string): ChatSlot => ({
  key,
  title,
  messages: 8,
  running: false,
  agent: 'kirocrew',
  last_message: msg,
  last_ts: lastTs,
} as ChatSlot)

/** date-desc: rebase (2m), migration (18m), flaky (1h), onboarding (3h). */
const REST: ChatSlot[] = [
  slot('chat-rebase', 'Rebase the release branch', at(2 * MIN), 'Rebased cleanly onto main.'),
  slot('chat-migration', 'Draft the migration notes', at(18 * MIN), 'Outline is ready for review.'),
  slot('chat-flaky', 'Triage flaky pipeline tests', at(60 * MIN), 'Two suites still intermittent.'),
  slot('chat-onboarding', 'Update the onboarding guide', at(180 * MIN), 'Rewrote the setup section.'),
]

/** The two OLDEST sessions go active, so date-desc wants them at the top —
 *  above `chat-migration`, which is the row the pointer is resting on. */
const BUMPED: ChatSlot[] = [
  REST[0],
  REST[1],
  slot('chat-flaky', 'Triage flaky pipeline tests', at(10_000), 'Two suites still intermittent.'),
  slot('chat-onboarding', 'Update the onboarding guide', at(20_000), 'Rewrote the setup section.'),
]

function Harness() {
  const [slots, setSlots] = useState(REST)
  const bump = useCallback(() => setSlots(BUMPED), [])
  useEffect(() => {
    // Without this every row takes the `!connected` branch — opacity-50,
    // cursor-not-allowed, and NO `hover:bg-bg-hover` — so the hovered row would
    // be photographed with no hover affordance at all, and the frame would show
    // a disabled sidebar rather than the one a user is pointing at.
    store.dispatch(sseConnected())
    ;(window as unknown as { __hoverHoldBump: () => void }).__hoverHoldBump = bump
  }, [bump])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={slots}
        activeSlot={null}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[{ name: 'kirocrew', description: 'Kiro Crew' }]}
      />
    </div>
  )
}

initI18n()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
