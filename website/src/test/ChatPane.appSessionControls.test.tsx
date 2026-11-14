import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

/* A Bot's thread on the Members page is a ChatPane, and its composer is the only
 * one on that page. With `appSessionControls` the pane draws the app's chip and
 * opens the control against its OWN slot's session; without it (split-view
 * panes) nothing is drawn, because the chat page's dock already owns them. */

const CONTROL: ResolvedSessionControl = {
  key: 'voice-app:talk', appName: 'voice-app', appDisplayName: 'Voice App', appVersion: '0.1.0',
  id: 'talk', entryPoint: 'dist/session-control.mjs', label: 'Talk', icon: 'AudioLines',
  allowedApi: [], allowedEvents: [], statusPath: '', processBacked: false,
}

vi.mock('../hooks/useSessionControls', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useSessionControls')>()),
  useSessionControls: () => ({ controls: [CONTROL], error: null }),
}))
vi.mock('../components/SessionControlHost', () => ({
  default: ({ session }: { session: { sessionKey: string } }) => (
    <div data-testid="session-control-host">{session.sessionKey}</div>
  ),
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    recording: false, transcribing: false, sessionOwner: null, streamEnabled: false,
    toggle: vi.fn(), start: vi.fn(), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
  }),
  voiceInputSupported: false,
}))
vi.mock('../hooks/usePushToTalk', () => ({ usePushToTalk: () => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }) }))
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    chatFolders: vi.fn().mockResolvedValue([]),
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {},
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'member-mate-thread'

async function renderPane(appSessionControls: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, project: '/repo', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} appSessionControls={appSessionControls} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
}

beforeEach(() => { localStorage.clear() })

describe('ChatPane app session controls', () => {
  it('opens the control against the pane slot session', async () => {
    await renderPane(true)
    const chip = screen.getByRole('button', { name: 'Talk' })
    await act(async () => { fireEvent.click(chip) })
    expect(screen.getByTestId('session-control-host').textContent).toBe(`dashboard:${SLOT}`)
    // A second click on the open chip closes it.
    await act(async () => { fireEvent.click(chip) })
    expect(screen.queryByTestId('session-control-host')).toBeNull()
  })

  it('draws no chip when the host does not opt in', async () => {
    await renderPane(false)
    expect(screen.queryByRole('button', { name: 'Talk' })).toBeNull()
  })
})
