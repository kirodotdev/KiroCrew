import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The Dynamic Dashboard dock a pane renders above its composer is a Feature
 * Preview (`PREVIEW_DASHBOARD`). A host may wire `onOpenCommandCenter` whatever
 * the flag says; the pane itself must still offer the dock only once the user
 * turned the preview on, and drop it in the same tick it is turned off. The dock
 * is stubbed: what is pinned here is the gate, not the dock's own behaviour. */
vi.mock('../pages/chat/command-center/CommandCenterDock', () => ({
  default: () => <div data-testid="command-center-dock-stub" />,
}))

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { PREVIEW_DASHBOARD, setPreviewFlag } from '../utils/previewFlags'

const SLOT = 'chat-1-dashboard-preview'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: SLOT, slotState: 'idle', messages: [] } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
}

function mount(onOpenCommandCenter?: () => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} onOpenCommandCenter={onOpenCommandCenter} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ChatPane dock behind the Dynamic Dashboard preview', () => {
  beforeEach(() => { localStorage.clear() })

  it('renders no dock while the preview is off, even when the host wired an opener', () => {
    mount(() => {})
    expect(screen.queryByTestId('command-center-dock-stub')).toBeNull()
  })

  it('renders the dock once the preview is on, and drops it in the same tick it goes off', () => {
    localStorage.setItem(PREVIEW_DASHBOARD, '1')
    mount(() => {})
    expect(screen.getByTestId('command-center-dock-stub')).toBeInTheDocument()
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, false) })
    expect(screen.queryByTestId('command-center-dock-stub')).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, true) })
    expect(screen.getByTestId('command-center-dock-stub')).toBeInTheDocument()
  })

  it('still needs a host opener: a pane without one renders no dock whatever the flag', () => {
    localStorage.setItem(PREVIEW_DASHBOARD, '1')
    mount(undefined)
    expect(screen.queryByTestId('command-center-dock-stub')).toBeNull()
  })
})
