/**
 * Test: the sidebar PR/MR chip is a real link.
 *
 * The chip used to be a plain <span> with the URL only in its tooltip, so the
 * PR it names was not reachable from the sidebar. It is now an <a> that opens
 * the pull request in a new tab. Because the session row itself is a
 * click-to-switch button, the anchor must also stop the click from bubbling —
 * otherwise opening the PR would switch sessions at the same time.
 *
 * Mock setup mirrors ChatSidebar.offline.test.tsx: the chat slice's switchSlot
 * thunk is mocked so we can assert whether a click reached the row handler.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { switchSlotMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: (...args: unknown[]) => switchSlotMock(...args) }
})

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: Object.fromEntries(
      [
        'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
        'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
        'renameSlot', 'forkSession', 'chatTags', 'chatFolders',
      ].map(k => [k, vi.fn().mockResolvedValue({})]),
    ),
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/634'

const slots = [
  { key: 's1', title: 'Other', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
  {
    key: 's2', title: 'PR session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
    source_links: [{ provider: 'github', number: 634, url: PR_URL, state: 'open', ci: 'passed' }],
    source_links_total: 1,
  },
] as unknown as ChatSlot[]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={'s1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const chip = () => screen.getByTitle(`Open ${PR_URL}`)

describe('ChatSidebar – PR chip link', () => {
  beforeEach(() => switchSlotMock.mockClear())

  it('renders the chip as an anchor that opens the pull request in a new tab', () => {
    renderSidebar()
    const a = chip()
    expect(a.tagName).toBe('A')
    expect(a).toHaveAttribute('href', PR_URL)
    expect(a).toHaveAttribute('target', '_blank')
    expect(a.getAttribute('rel')).toContain('noopener')
    expect(a).toHaveTextContent('#634')
  })

  it('clicking the chip does NOT switch sessions, while clicking the row still does', () => {
    renderSidebar()
    // Positive control first: the row handler IS reachable in this harness, so
    // the negative assertion below is meaningful and not vacuous.
    const row = chip().closest('.session-row') as HTMLElement
    fireEvent.click(row)
    expect(switchSlotMock).toHaveBeenCalledWith('s2')

    switchSlotMock.mockClear()
    fireEvent.click(chip())
    expect(switchSlotMock).not.toHaveBeenCalled()
  })
})
