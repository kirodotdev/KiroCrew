/**
 * Test: the sidebar PR/MR chip is a real link.
 *
 * The chip is an <a> that opens the pull request in a new tab, so the PR it
 * names is reachable from the sidebar. Because the session row itself is a
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

function renderSidebar(rows: ChatSlot[] = slots) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots: rows,
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
              slots={rows} activeSlot={'s1'} unreadSlots={[]}
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

/**
 * A terminal pull request can never merge, so its CI rollup is moot and only the
 * lifecycle glyph is meaningful. `closed` is the case that actually hangs: a PR
 * closed before its checks were approved to run keeps a PENDING rollup forever,
 * which the backend faithfully projects as `ci: "running"` — so a chip gated
 * only on `merged` spins its spinner indefinitely on work nobody is waiting for.
 *
 * The `merged` half is asserted here too. Both terminal states plus a live
 * control live in one table.
 */
describe('ChatSidebar – terminal PR chips suppress CI', () => {
  const url = (n: number) => `https://github.com/kirodotdev/KiroCrew/pull/${n}`

  function stateRows(): ChatSlot[] {
    return [
      { key: 's1', title: 'Other', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
      {
        key: 's2', title: 'PR states', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
        source_links: [
          // Every chip carries ci: 'running' so the ONLY variable is `state`.
          { provider: 'github', number: 993, url: url(993), state: 'closed', ci: 'running' },
          { provider: 'github', number: 994, url: url(994), state: 'merged', ci: 'running' },
          { provider: 'github', number: 995, url: url(995), state: 'open', ci: 'running' },
          // No `state` at all: the provider status has not been read yet, which
          // is NOT terminal — CI must still render.
          { provider: 'github', number: 996, url: url(996), ci: 'running' },
        ],
        source_links_total: 4,
      },
    ] as unknown as ChatSlot[]
  }

  const spinner = (n: number) =>
    screen.getByTitle(`Open ${url(n)}`).querySelector('[aria-label="Checks running"]')

  it.each([
    ['closed', 993],
    ['merged', 994],
  ])('hides the running-checks spinner on a %s chip', (_state, number) => {
    renderSidebar(stateRows())
    expect(spinner(number)).toBeNull()
  })

  it('still shows the spinner while the PR is live or its state is unknown', () => {
    renderSidebar(stateRows())
    // Positive control: proves the fixture really does carry ci: 'running' and
    // the assertions above are not passing because nothing rendered.
    expect(spinner(995)).not.toBeNull()
    expect(spinner(996)).not.toBeNull()
  })

  it('keeps the closed chip\'s own lifecycle label', () => {
    renderSidebar(stateRows())
    // The spinner goes away; the terminal signal must not.
    expect(screen.getByTitle(`Open ${url(993)}`)).toHaveTextContent('closed')
    expect(screen.getByTitle(`Open ${url(994)}`).querySelector('[aria-label="Merged"]')).not.toBeNull()
  })

  it.each(['passed', 'failed'] as const)('hides a %s CI glyph on a closed chip too', (ci) => {
    const rows = [
      { key: 's1', title: 'Other', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
      {
        key: 's2', title: 'PR states', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
        source_links: [{ provider: 'github', number: 993, url: url(993), state: 'closed', ci }],
        source_links_total: 1,
      },
    ] as unknown as ChatSlot[]
    renderSidebar(rows)
    const chipEl = screen.getByTitle(`Open ${url(993)}`)
    expect(chipEl.querySelector('[aria-label="Checks passed"]')).toBeNull()
    expect(chipEl.querySelector('[aria-label="Checks failed"]')).toBeNull()
  })
})
