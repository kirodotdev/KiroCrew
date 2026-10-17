/**
 * Test: sidebar source-link chips are project-qualified when a session's links
 * span more than one project, and keep the bare label (#N) when they do not.
 *
 * The bug (issue #9782): `group-a/svc#1` and `group-b/svc#1` both rendered
 * as `#1` because the chip used `link.label ?? '#N'` (the backend's per-scope
 * identifier) with no project prefix. PR #9769 fixed the same collision for the
 * Changes/Issues panel tabs via `sourceTabQualifier`; this fix applies the same
 * helper to the sidebar chips.
 *
 * Scenarios pinned here:
 *   a. Two projects, same issue #1 → chips show `group-a/svc #1` and
 *      `group-b/svc #1` (distinct, qualified).
 *   b. One project, two items (#1 and #2) → chips show bare `#1` and `#2`
 *      (no qualifier added, short labels preserved).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return {
    ...actual,
    switchSlot: (...args: unknown[]) =>
      vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} }))(...args),
  }
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

/** GitHub URLs for two different projects, both with issue #1. */
const URL_A1 = 'https://github.com/group-a/svc/issues/1'
const URL_B1 = 'https://github.com/group-b/svc/issues/1'
/** Two items in the same project. */
const URL_A1_CHANGE = 'https://github.com/group-a/svc/pull/1'
const URL_A2_CHANGE = 'https://github.com/group-a/svc/pull/2'

/** The active slot has no chips to keep assertions focused on the test row. */
const activeSlot: ChatSlot = {
  key: 'active', title: 'Active', messages: 1, running: false,
  mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
  source_links: [], source_links_total: 0,
} as unknown as ChatSlot

function renderWithSlots(rows: ChatSlot[]) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots: rows,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [],
      updateProgress: null, subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear', slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'active',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
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
              slots={rows} activeSlot="active" unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="default" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatSidebar – sidebar chip project qualifier', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    // Disable the stale-session collapse so the test rows render immediately.
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
  })
  afterEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  it('qualifies chips when a session links span two different GitHub projects with the same number', () => {
    // Both group-a/svc and group-b/svc have issue #1.
    const slot: ChatSlot = {
      key: 'multi-project', title: 'Two-project session',
      messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
      source_links: [
        { provider: 'github', number: 1, label: '#1', url: URL_A1, state: 'open', kind: 'issue' },
        { provider: 'github', number: 1, label: '#1', url: URL_B1, state: 'open', kind: 'issue' },
      ],
      source_links_total: 2,
    } as unknown as ChatSlot
    renderWithSlots([activeSlot, slot])

    // Both chips are rendered — the two URLs produce distinct DOM elements.
    const chipA = screen.getByTitle(new RegExp(URL_A1))
    const chipB = screen.getByTitle(new RegExp(URL_B1))

    // Each chip must carry its project path so they are distinguishable.
    expect(chipA.textContent).toContain('group-a/svc')
    expect(chipB.textContent).toContain('group-b/svc')
    // The number label stays on each chip too.
    expect(chipA.textContent).toContain('#1')
    expect(chipB.textContent).toContain('#1')
    // The two chips are distinct — not both showing just "#1".
    expect(chipA.textContent).not.toBe(chipB.textContent)
  })

  it('keeps bare chip labels when all of a session\'s links come from one project', () => {
    // Both PRs are in group-a/svc — no collision, no qualifier needed.
    const slot: ChatSlot = {
      key: 'single-project', title: 'One-project session',
      messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
      source_links: [
        { provider: 'github', number: 1, label: '#1', url: URL_A1_CHANGE, state: 'open', kind: 'change' },
        { provider: 'github', number: 2, label: '#2', url: URL_A2_CHANGE, state: 'open', kind: 'change' },
      ],
      source_links_total: 2,
    } as unknown as ChatSlot
    renderWithSlots([activeSlot, slot])

    const chip1 = screen.getByTitle(new RegExp(URL_A1_CHANGE))
    const chip2 = screen.getByTitle(new RegExp(URL_A2_CHANGE))

    // Bare labels — no project path prefix.
    expect(chip1.textContent?.trim()).toBe('#1')
    expect(chip2.textContent?.trim()).toBe('#2')
  })
})
