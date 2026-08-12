/**
 * Test: a worktree-bound session shows its branch in the sidebar, and shows it
 * as trailing detail ON the agent/meta line rather than as a new stacked line.
 *
 * The placement is the point, not decoration. A chat session list item has a
 * FIXED set of stacked lines (AUTOSDE `session-row-fixed-height`, blocking, and
 * it names ChatSidebar.tsx): the sidebar width is user-controlled and the list
 * must not scroll horizontally, so per-session metadata folds into a line that
 * already exists. Asserting only "the branch is visible" would pass just as
 * happily with the extra row that rule forbids, so this pins the ancestor.
 *
 * Mock setup mirrors ChatSidebar.channelFolder.test.tsx.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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

const slots = [
  {
    key: 'dashboard_chat-1-1', title: 'In a tree', messages: 1, running: false, mode: '',
    created: '', last_ts: '2026-01-01T00:00:00Z',
    worktree: { repo: '/repo', branch: 'feat/tidy-panel', base: 'main', path: '/repo/wt-tidy' },
  },
  {
    key: 'dashboard_chat-1-2', title: 'Plain session', messages: 1, running: false, mode: '',
    created: '', last_ts: '2026-01-01T00:00:00Z',
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
      activeSlot: 'dashboard_chat-1-1',
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
              slots={slots} activeSlot={'dashboard_chat-1-1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatSidebar – worktree branch chip', () => {
  it('shows the branch of a worktree-bound session', () => {
    renderSidebar()
    expect(screen.getByText('feat/tidy-panel')).toBeInTheDocument()
  })

  it('carries it on the agent/meta line, not a new stacked line', () => {
    renderSidebar()
    const chip = screen.getByText('feat/tidy-panel')
    expect(chip.closest('.session-agent-label')).not.toBeNull()
  })

  it('shows nothing for a session that is not in a worktree', () => {
    renderSidebar()
    // Only the one bound session contributes a branch chip.
    expect(screen.queryAllByText('feat/tidy-panel')).toHaveLength(1)
  })
})
