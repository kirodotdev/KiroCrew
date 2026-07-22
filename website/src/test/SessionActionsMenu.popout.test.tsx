/**
 * Regression tests for the session menu's popout items rendered INSIDE a
 * popout window (review). The header menu re-renders inside the
 * popout (`ChatPage embedMode="chat"` includes `ChatHeaderMenu`), but a popout
 * never holds its OWN slot in the coordination map (BroadcastChannel doesn't
 * self-deliver), so keying purely off `isPoppedOut` would wrongly offer
 * "Pop out to window" for the session that is already this window — and
 * clicking it would `window.open` into the popout's own window name, reloading
 * it in place. The menu must key off `isSelfPopout` and offer only
 * "Bring back to main" there.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import { ChatHeaderMenu } from '../pages/ChatPage'
import { registerPopout, __resetForTests, __setNavigateForTests } from '../utils/chatPopout'

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as any

const slot = { key: 'chat-1', title: 'My Session' } as any

function renderMenu() {
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ ...slot }] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot={slot.key} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Radix DropdownMenuTrigger opens on keyboard activation — the path jsdom
  // handles, unlike the PointerEvent-driven mouse open.
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return utils
}

beforeEach(() => {
  __resetForTests()
  vi.clearAllMocks()
})

afterEach(() => {
  __resetForTests()
  vi.restoreAllMocks()
})

describe('SessionActionsMenu popout items', () => {
  it('main window: offers "Pop out to window" for a not-popped-out session', async () => {
    renderMenu()
    expect(await screen.findByText('Pop out to window')).toBeTruthy()
    expect(screen.queryByText('Bring back to main')).toBeNull()
  })

  it('inside the popout window: offers only "Bring back to main" for its own session', async () => {
    registerPopout(slot.key) // this window IS the popout for chat-1
    renderMenu()
    expect(await screen.findByText('Bring back to main')).toBeTruthy()
    expect(screen.queryByText('Pop out to window')).toBeNull()
    expect(screen.queryByText('Focus popped-out window')).toBeNull()
  })

  it('inside the popout: "Bring back to main" runs the deep-link-safe return path', async () => {
    registerPopout(slot.key)
    vi.spyOn(window, 'close').mockImplementation(() => {}) // jsdom window stays open → fallback navigates
    const navigated: string[] = []
    __setNavigateForTests(url => navigated.push(url))
    renderMenu()
    fireEvent.click(await screen.findByText('Bring back to main'))
    expect(navigated).toEqual(['/chat?sid=chat-1'])
  })
})
