import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* chat-core P3-b (#9775): ChatPane mounts the same ChatInput as ChatPage but
 * had no microphone — dictation arrived at ChatInput as 23 host-wired props the
 * pane never passed. Voice is now the root-mounted Voice atom: the pane wraps its
 * ChatInput in a `<Composer>` root and the mic appears with no voice props at
 * all. These tests pin that: the REAL ChatInput renders inside the pane, the mic
 * button exists, and pressing it reaches the shared engine.
 *
 * P3-d (#9808): the `@` file/folder menu reaches the pane the same way, through
 * the root's Mention atom, with no picker props on the pane's ChatInput. */

const engine = {
  recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
  toggle: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
  error: null as string | null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
  download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
}
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => engine, voiceInputSupported: true }))
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
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockImplementation((_q: string, project?: string) => Promise.resolve({
      root: project || '/repo',
      results: [
        { path: '/repo/src/widgets', name: 'widgets', size: 0, mtime: 1750000000, kind: 'dir' },
        { path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: 1750000000, kind: 'file' },
      ],
    })),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    dashboardConfig: vi.fn().mockResolvedValue({ quick_send: false }),
    sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
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
import { api } from '../api/client'

const SLOT = 'chat-1-parity'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, project: '/repo', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The STT config is what lets the mic START (an unloaded config opens the
  // setup modal instead); seed it so the press below reaches the engine.
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' })
  const store = makeStore(SLOT)
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  await waitFor(() => expect(screen.getAllByRole('textbox').length).toBeGreaterThan(0))
  return store
}

const mic = () => screen.getByRole('button', { name: /^Voice input/ })

beforeEach(() => { localStorage.clear(); engine.start.mockClear(); vi.mocked(api.sendChat).mockClear() })

describe('ChatPane composer parity (chat-core P3-b): voice through the Composer root', () => {
  it('renders the microphone with no voice props on the pane', async () => {
    await renderPane()
    expect(mic()).toBeTruthy()
  })

  it('pressing the mic starts the shared engine (the atom, not a dead prop)', async () => {
    await renderPane()
    await act(async () => { fireEvent.click(mic()) })
    await waitFor(() => expect(engine.start).toHaveBeenCalledTimes(1))
  })
})

describe('ChatPane composer parity (chat-core P3-d): @ mentions through the Composer root', { timeout: 15_000 }, () => {
  const box = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
  /** Type an @-token and wait out the search debounce for the row named `name`. */
  async function search(query: string, name: string) {
    fireEvent.change(box(), { target: { value: query } })
    return screen.findByText(name, undefined, { timeout: 3000 })
  }

  it('a picked file stages a chip whose remove takes its @token out of the text', async () => {
    await renderPane()
    const row = await search('@mai', 'main.ts')
    // A pane-width menu cuts each row's folder from the start, so the end that
    // tells sibling rows apart stays in view.
    const folders = screen.getAllByTestId('file-picker-row-folder')
    expect(folders.map(f => [f.textContent, f.getAttribute('dir')])).toEqual([['src/', 'rtl'], ['src/', 'rtl']])
    fireEvent.mouseDown(row)
    await waitFor(() => expect(box().value).toBe('@src/main.ts '))
    fireEvent.change(box(), { target: { value: 'see @src/main.ts now' } })
    const chip = screen.getByRole('group', { name: '/repo/src/main.ts' })
    fireEvent.click(within(chip).getByRole('button', { name: 'Remove' }))
    expect(screen.queryByRole('group', { name: '/repo/src/main.ts' })).toBeNull()
    expect(box().value).toBe('see now')
  })

  it('a picked folder serializes against the pane project on send', async () => {
    await renderPane()
    fireEvent.mouseDown(await search('@wid', 'widgets/'))
    await screen.findByLabelText('Remove folder')
    fireEvent.change(box(), { target: { value: `${box().value}summarize it` } })
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wire, , , , meta] = vi.mocked(api.sendChat).mock.calls[0]
    expect(wire).toContain('[attached_dir 1] /repo/src/widgets')
    expect(wire).not.toContain('@src/widgets/')
    expect(meta?.dirs).toEqual(['/repo/src/widgets'])
  })
})
