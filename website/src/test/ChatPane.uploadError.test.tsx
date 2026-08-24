import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* #5707: a server-refused upload (unsupported type, signature mismatch,
 * over-cap) resolves as `{ paths: [], error }` — api.uploadFiles does NOT
 * throw — so ChatPane's upload mutation used to land in onSuccess, find no
 * paths, and do nothing: the spinner stopped and the user saw no attachment
 * and no message. ChatPane now surfaces res.error the way ChatPage does, and
 * reports its client-side refusals too. No silent refusal path is left. */

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
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }, { name: 'reviewer' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane upload — a refused upload is surfaced, not silent (#5707)', () => {
  it('renders the server error when uploadFiles resolves { paths: [], error }', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      paths: [], error: 'Unsupported file type: application/x-msdownload',
    })
    const { container } = renderPane('pane-refused')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'evil.exe', { type: 'application/x-msdownload' })
    Object.defineProperty(fileInput, 'files', { value: [file] })
    fireEvent.change(fileInput)

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    // Before the fix this text never appeared — onSuccess ignored res.error.
    await waitFor(() =>
      expect(screen.getByText(/Unsupported file type: application\/x-msdownload/)).toBeInTheDocument(),
    )
  })

  it('renders the pane\'s connectivity copy when the upload fetch rejects (onError path)', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { container } = renderPane('pane-threw')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'clip.png', { type: 'image/png' })
    Object.defineProperty(fileInput, 'files', { value: [file] })
    fireEvent.change(fileInput)

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    // A transport reject must not leak "Failed to fetch" to the user, and must
    // not claim a 50 MB ceiling either.
    await waitFor(() => expect(screen.getByText(/Connection error/i)).toBeInTheDocument())
    expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument()
    expect(screen.queryByText(/max 50 MB/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument()
  })

  it('passes a non-transport error\'s own message through (resize / session expiry)', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('Session expired'))
    const { container } = renderPane('pane-threw-msg')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'clip.png', { type: 'image/png' })
    Object.defineProperty(fileInput, 'files', { value: [file] })
    fireEvent.change(fileInput)

    await waitFor(() => expect(screen.getByText(/Session expired/)).toBeInTheDocument())
  })

  it('reports a >20-file drop at the banner without calling the server', async () => {
    const { container } = renderPane('pane-toomany')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const files = Array.from({ length: 21 }, (_, i) => new File(['x'], `f${i}.png`, { type: 'image/png' }))
    Object.defineProperty(fileInput, 'files', { value: files })
    fireEvent.change(fileInput)

    // This guard used to `return` silently: 21 files vanished with no message.
    await waitFor(() => expect(screen.getByText(/Too many files/i)).toBeInTheDocument())
    expect(api.uploadFiles).not.toHaveBeenCalled()
  })

  it('reports an oversized document, and exempts video so its own 413 reports the real cap', async () => {
    const bigDoc = new File(['x'], 'huge.png', { type: 'image/png' })
    Object.defineProperty(bigDoc, 'size', { value: 60 * 1024 * 1024 })
    const { container, unmount } = renderPane('pane-bigdoc')
    let fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [bigDoc] })
    fireEvent.change(fileInput)

    // Was silent before; 50 MB is the true ceiling for a document, so the
    // message can state it.
    await waitFor(() => expect(screen.getByText(/File too large: huge\.png/)).toBeInTheDocument())
    expect(api.uploadFiles).not.toHaveBeenCalled()
    unmount()

    // A recording is exempt from the client guard at ANY size, so it reaches
    // the server and an over-cap one is refused by its own 413 -- which states
    // the real video ceiling instead of this message's 50 MB.
    const bigVideo = new File(['x'], 'screencap.mp4', { type: 'video/mp4' })
    Object.defineProperty(bigVideo, 'size', { value: 600 * 1024 * 1024 })
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      paths: [], error: 'Video exceeds the 512 MB limit',
    })
    const second = renderPane('pane-bigvideo')
    fileInput = second.container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [bigVideo] })
    fireEvent.change(fileInput)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/Video exceeds the 512 MB limit/)).toBeInTheDocument())
    expect(screen.queryByText(/max 50 MB/)).not.toBeInTheDocument()
  })

  it('clears a previous refusal on the next attempt, so it cannot misattribute', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: [], error: 'Unsupported file type' })
    const { container } = renderPane('pane-stale')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const bad = new File(['x'], 'evil.exe', { type: 'application/x-msdownload' })
    Object.defineProperty(fileInput, 'files', { value: [bad], configurable: true })
    fireEvent.change(fileInput)
    await waitFor(() => expect(screen.getByText(/Unsupported file type/)).toBeInTheDocument())

    // A SUCCEEDING upload is the case nothing else clears: onSuccess appends
    // paths and sets no message, so without the clear at entry the previous
    // refusal keeps standing over a completed attach.
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/ok.png'] })
    const ok = new File(['x'], 'ok.png', { type: 'image/png' })
    Object.defineProperty(fileInput, 'files', { value: [ok], configurable: true })
    fireEvent.change(fileInput)

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText(/Unsupported file type/)).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
  })

  it('shows no error banner on a successful upload', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/ok.png'] })
    const { container } = renderPane('pane-ok')
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'ok.png', { type: 'image/png' })
    Object.defineProperty(fileInput, 'files', { value: [file] })
    fireEvent.change(fileInput)

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
  })
})
