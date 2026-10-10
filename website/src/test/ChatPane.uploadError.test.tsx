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
    dashboardConfig: vi.fn().mockResolvedValue({}),
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
    // not claim a size ceiling either.
    await waitFor(() => expect(screen.getByText(/Connection error/i)).toBeInTheDocument())
    expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument()
    expect(screen.queryByText(/max \d+ MB/)).not.toBeInTheDocument()
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

  it('reports a >20-file drop as a hint without calling the server', async () => {
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
    Object.defineProperty(bigDoc, 'size', { value: 101 * 1024 * 1024 })
    const { container, unmount } = renderPane('pane-bigdoc')
    await waitFor(() => expect(api.dashboardConfig).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    let fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, 'files', { value: [bigDoc] })
    fireEvent.change(fileInput)

    // The config answered without a figure, so the cap is the config default
    // (100 MB), the true ceiling for a document, and the message can state it.
    await waitFor(() => expect(screen.getByText(/File too large: huge\.png \(max 100 MB\)/)).toBeInTheDocument())
    expect(api.uploadFiles).not.toHaveBeenCalled()
    unmount()

    // A recording is exempt from the client guard at ANY size, so it reaches
    // the server and an over-cap one is refused by its own 413 -- which states
    // the real video ceiling instead of this message's document cap.
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
    expect(screen.queryByText(/max \d+ MB/)).not.toBeInTheDocument()
  })

  it('pre-checks against the gateway-served upload_max_mb and names it', async () => {
    ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockResolvedValue({ upload_max_mb: 200 })
    try {
      // 150 MB: over the old fixed 50 MB line and the 100 MB default, under the
      // configured 200 -- reaches the server.
      const okZip = new File(['x'], 'project.zip', { type: 'application/zip' })
      Object.defineProperty(okZip, 'size', { value: 150 * 1024 * 1024 })
      const first = renderPane('pane-cfg-ok')
      await waitFor(() => expect(api.dashboardConfig).toHaveBeenCalled())
      // Let the config query settle before the pick, as a real user would.
      await new Promise(r => setTimeout(r, 0))
      let fileInput = first.container.querySelector('input[type="file"]') as HTMLInputElement
      Object.defineProperty(fileInput, 'files', { value: [okZip] })
      ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockClear()
      fireEvent.change(fileInput)
      await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
      first.unmount()

      const bigZip = new File(['x'], 'game.zip', { type: 'application/zip' })
      Object.defineProperty(bigZip, 'size', { value: 250 * 1024 * 1024 })
      const second = renderPane('pane-cfg-big')
      await waitFor(() => expect(api.dashboardConfig).toHaveBeenCalled())
      await new Promise(r => setTimeout(r, 0))
      fileInput = second.container.querySelector('input[type="file"]') as HTMLInputElement
      Object.defineProperty(fileInput, 'files', { value: [bigZip] })
      ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockClear()
      fireEvent.change(fileInput)
      await waitFor(() => expect(screen.getByText(/File too large: game\.zip \(max 200 MB\)/)).toBeInTheDocument())
      // A pre-check refusal is a validation hint (nothing was sent), not a
      // failure: it renders as a status notice, never through the error banner.
      expect(screen.getByTestId('chat-pane-upload-hint')).toHaveAttribute('role', 'status')
      expect(screen.getByTestId('chat-pane-upload-hint')).toHaveTextContent(/max 200 MB/)
      expect(screen.queryByTestId('chat-pane-upload-error')).not.toBeInTheDocument()
      expect(api.uploadFiles).not.toHaveBeenCalled()
    } finally {
      ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    }
  })

  it('skips the pre-check until the config answers, so an early drop is judged by the server', async () => {
    let release: (v: unknown) => void = () => {}
    ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(r => { release = r }))
    try {
      const zip = new File(['x'], 'early.zip', { type: 'application/zip' })
      Object.defineProperty(zip, 'size', { value: 150 * 1024 * 1024 })
      const { container } = renderPane('pane-cfg-pending')
      const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
      Object.defineProperty(fileInput, 'files', { value: [zip] })
      ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockClear()
      fireEvent.change(fileInput)
      // No figure is known yet: the 100 MB default is not presented as the
      // gateway's limit, so the file goes to the server, which enforces it.
      await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
      expect(screen.queryByText(/File too large/)).not.toBeInTheDocument()
    } finally {
      release({})
      ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    }
  })

  it('reports a failed settings read instead of silently using a default limit', async () => {
    ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('HTTP 502'))
    try {
      renderPane('pane-cfg-failed')
      expect(await screen.findByTestId('chat-pane-upload-limit-error', {}, { timeout: 5000 })).toHaveTextContent(
        /file sizes are checked only when the upload reaches the server/,
      )
    } finally {
      ;(api.dashboardConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    }
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
