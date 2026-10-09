import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* ChatPane must hand its host's file viewer to the transcript (#9487, #14458).
 * A path a crewmate names in its reply, or a non-image attachment on a sent
 * prompt, opens the file the way it does on the main chat page. Before this
 * the pane never passed `onFileOpen` to ChatMessageList: the Members page
 * handed the pane nothing it could forward, the markdown renderer saw no
 * handler and a confirmed path chip fell back to the OS file-manager reveal,
 * which on a remote gateway answered the click with nothing. Pinned here at
 * the pane boundary: the host handler is what the row's renderer receives. */

/** The file the crewmate's reply names. Hoisted: the renderer stand-in in the
 *  `vi.mock` factory below closes over it, and `vi.mock` is lifted above every
 *  import, so an ordinary `const` would not yet exist when the factory runs. */
const { REPLY_PATH } = vi.hoisted(() => ({ REPLY_PATH: '/home/me/workspace/notes/hld.md' }))

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
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
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
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], defaultAgent: 'default' }) }))
// The real renderer upgrades a span to a path chip only after a backend stat
// probe confirms the path; what this test pins is the hop BEFORE that — whether
// the pane's host handler reaches the renderer at all. So the stand-in draws
// one chip per reply that activates exactly the way `activatePath` does with a
// wired handler, and draws plain text when no handler arrived.
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content, onFileOpen }: { content: string; onFileOpen?: (path: string) => void }) => (
    onFileOpen
      ? <button type="button" data-testid="path-chip" onClick={() => onFileOpen(REPLY_PATH)}>{content}</button>
      : <span data-testid="markdown">{content}</span>
  ),
}))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

/** A sent prompt with a non-image attachment, then the crewmate's reply naming a file. */
const MESSAGES = [
  {
    role: 'user',
    content: 'summarize this',
    cls: '',
    ts: '2026-09-08T08:00:00Z',
    meta: { files: ['/tmp/report.pdf'] },
  },
  {
    role: 'assistant',
    content: `The draft is at \`${REPLY_PATH}\`.`,
    cls: '',
    ts: '2026-09-08T08:00:30Z',
  },
]

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: MESSAGES.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(slotKey: string, extraProps: Record<string, unknown> = {}) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({ messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  await act(async () => {
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={slotKey} {...extraProps} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    )
  })
  // Hydration is settled once the reply has painted.
  await waitFor(() => expect(screen.getByText(/The draft is at/)).toBeTruthy())
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane file open (#9487 / #14458)', () => {
  it("a crewmate's reply opens a named file through the host handler", async () => {
    const onFileOpen = vi.fn()
    await renderPane('pane-file-1', { onFileOpen, busyMode: 'steer-only', frameless: true, agentLocked: true, crewmate: { name: 'Conductor' } })
    fireEvent.click(screen.getByTestId('path-chip'))
    expect(onFileOpen).toHaveBeenCalledWith(REPLY_PATH)
  })

  it('a split-pane reply opens a named file through the host handler', async () => {
    const onFileOpen = vi.fn()
    await renderPane('pane-file-2', { onFileOpen })
    fireEvent.click(screen.getByTestId('path-chip'))
    expect(onFileOpen).toHaveBeenCalledWith(REPLY_PATH)
  })

  it('a sent prompt’s attachment card opens through the host handler, in a member DM too', async () => {
    const onFileOpen = vi.fn()
    await renderPane('pane-file-3', { onFileOpen, busyMode: 'steer-only', frameless: true, agentLocked: true, crewmate: { name: 'Conductor' } })
    fireEvent.click(screen.getByRole('button', { name: /report\.pdf/ }))
    expect(onFileOpen).toHaveBeenCalledWith('/tmp/report.pdf')
  })

  it('without a host handler the reply renders with no opener (capability by omission)', async () => {
    await renderPane('pane-file-4', { crewmate: { name: 'Conductor' } })
    expect(screen.queryByTestId('path-chip')).toBeNull()
    expect(screen.getByText(/The draft is at/)).toHaveTextContent(REPLY_PATH)
  })
})
