/**
 * Test: a folder created by per-channel session filing shows that channel's
 * brand mark in the sidebar, so "Discord" reads as the Discord conversations
 * rather than as an ordinary folder that happens to be named after an app.
 *
 * A folder with no `channel` stamp (every hand-made folder) is unchanged, and a
 * stamp with no brand asset shows nothing rather than the generic Link2
 * fallback, which means live mirroring elsewhere in this sidebar.
 *
 * Mock setup mirrors ChatSidebar.channelOriginGlyph.test.tsx.
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
import type { ChatFolder, ChatSlot } from '../types'
import type { RootState } from '../store'

const folders: ChatFolder[] = [
  { id: 'f-discord', name: 'Discord', order: 0, parent_id: '', channel: 'discord' },
  { id: 'f-plain', name: 'Work', order: 1, parent_id: '' },
  { id: 'f-brandless', name: 'Direct messages', order: 2, parent_id: '', channel: 'unified' },
]

const slots = [
  { key: 'discord_kirocrew_direct_U1', title: 'From Discord', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z', folder_id: 'f-discord' },
  { key: 'dashboard_chat-1-1', title: 'Plain dashboard', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z', folder_id: 'f-plain' },
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
      activeSlot: 'discord_kirocrew_direct_U1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={'discord_kirocrew_direct_U1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The folder header row that owns a given folder name. */
const header = (name: string) =>
  screen.getByText(name).closest('button') as HTMLElement

describe('ChatSidebar – channel session folder', () => {
  it('shows the channel brand mark on a channel-owned folder', () => {
    renderSidebar()
    expect(header('Discord').querySelector('img')?.getAttribute('src')).toMatch(/discord/)
  })

  it('leaves a hand-made folder and a brandless stamp without a logo', () => {
    renderSidebar()
    expect(header('Work').querySelector('img')).toBeNull()
    // `unified` is the aggregated DM bucket — no brand asset, so no mark.
    expect(header('Direct messages').querySelector('img')).toBeNull()
  })
})
