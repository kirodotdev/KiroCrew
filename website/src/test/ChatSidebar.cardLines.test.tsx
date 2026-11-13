/**
 * Test: Settings → Chat → Sessions → "Title Lines" / "Preview Lines" decide how
 * many lines a session card's title and latest-message preview may wrap onto.
 *
 * One line keeps the single-line ellipsis everyone has today. Two or three
 * lines wrap and cut off after the last allowed line. A change applies to rows
 * already on screen, without a reload, and a stored value outside 1-3 falls
 * back to one line rather than leaving the text unbounded.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
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
import { loadChatConfig, saveChatConfig } from '../pages/chat/ChatSettings'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const TITLE = 'PR #1234: retry the upload when the storage token expires mid-request'
const PREVIEW = 'Fixed both table rows and re-uploaded the draft; the re-review is queued.'

const slots = [
  { key: 's1', title: TITLE, messages: 2, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z', last_message: PREVIEW },
] as unknown as ChatSlot[]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots,
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

const titleEl = () => screen.getByText(TITLE).closest('[data-session-title]') as HTMLElement
const previewEl = () => screen.getByText(PREVIEW).closest('[data-session-preview]') as HTMLElement

function setLines(title: unknown, preview: unknown) {
  saveChatConfig({ ...loadChatConfig(), sessionTitleLines: title, sessionPreviewLines: preview } as ReturnType<typeof loadChatConfig>)
}

describe('session card line counts', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => localStorage.clear())

  it('keeps the single-line ellipsis when nothing is set', () => {
    renderSidebar()
    expect(titleEl().className).toContain('truncate')
    expect(previewEl().className).toContain('truncate')
    expect(titleEl().className).not.toMatch(/line-clamp/)
  })

  it('wraps the title and the preview to the chosen counts, each on its own', () => {
    setLines(2, 3)
    renderSidebar()
    expect(titleEl().className).toContain('line-clamp-2')
    expect(titleEl().className).not.toContain('truncate')
    expect(previewEl().className).toContain('line-clamp-3')
    expect(previewEl().className).not.toContain('truncate')
  })

  it('applies a change to rows already on screen, without a reload', () => {
    renderSidebar()
    expect(titleEl().className).toContain('truncate')
    act(() => { setLines(3, 1) })
    expect(titleEl().className).toContain('line-clamp-3')
    expect(previewEl().className).toContain('truncate')
  })

  it.each([[0], [4], ['2'], [null], [1.5]])('falls back to one line for a stored %p', (bad) => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ sessionTitleLines: bad, sessionPreviewLines: bad }))
    const cfg = loadChatConfig()
    expect(cfg.sessionTitleLines).toBe(1)
    expect(cfg.sessionPreviewLines).toBe(1)
  })
})
