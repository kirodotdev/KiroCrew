/** Placement pin: AUTOSDE `session-row-fixed-height` (blocking) fixes how many lines a
 *  session row may carry, so the sidebar shows a duplicate refusal at its own root. */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import ChatSidebar from '../pages/ChatSidebar'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const SLOT = 'chat-1'

vi.mock('../hooks/useSessionActions', () => ({
  pinMutationKeysInFlight: new Set<string>(),
  useSessionActions: () => ({
    duplicate: vi.fn(),
    close: vi.fn(),
    forkError: {
      message: 'Too large to duplicate whole.',
      report: undefined,
      slotKey: SLOT,
    },
    clearForkError: vi.fn(),
  }),
}))

function mount() {
  const slots = [{ key: SLOT, title: 'Big session', running: false, tags: [], created: '', last_ts: '' }]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: SLOT } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={SLOT} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('sidebar duplicate refusal placement', () => {
  it('renders the refusal outside the scrolling session-row list', () => {
    mount()
    const notice = screen.getByTestId(`sidebar-duplicate-error-${SLOT}`)

    // Walking the ancestor chain is the assertion: in the row list the notice sits inside
    // the list's own scroll container, which is what charges every row for its height.
    let scrollingAncestors = 0
    for (let el = notice.parentElement; el; el = el.parentElement) {
      if (/overflow-y-auto/.test(el.className || '')) scrollingAncestors += 1
    }
    expect(scrollingAncestors).toBe(0)
  })
})
