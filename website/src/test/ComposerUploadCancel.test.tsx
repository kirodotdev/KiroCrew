/**
 * Cancelling a composer upload that is still in flight (issue #5744).
 *
 * The request used to run to completion whatever the user did: the only exit
 * from a slow transfer was reloading the page, which throws away the bytes
 * already sent. Both composer hosts now send one request per file and register
 * each request's AbortController, with the file's name, per slot
 * (composerSendHolds); ChatInput shows each as a chip with its own cancel.
 *
 * Two things are pinned per host, because the second is the trap: aborting must
 * end the request AND must not be dressed up as a failure. The blanket catch
 * would otherwise tell a user who cancelled a 150 MB recording to check the
 * file type and a 50 MB cap.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

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
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn(),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
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
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { cancelComposerUploads, isComposerSendHeld, UPLOAD_STALL_MS } from '../utils/composerSendHolds'

/** The DOMException `fetch` rejects with once its signal aborts. */
function abortError(): Error {
  const e = new Error('The operation was aborted.')
  e.name = 'AbortError'
  return e
}

/**
 * Stand in for an upload still on the wire: never settles on its own, and
 * rejects the way `fetch` does the moment the caller's signal aborts. Returns a
 * getter for the signal the host handed in, which is the only proof the host
 * wired one at all, and `progress`, which steps the bytes sent through the
 * host's own progress callback.
 */
function pendingUpload() {
  let captured: AbortSignal | undefined
  let report: ((loaded: number, total: number) => void) | undefined
  vi.mocked(api.uploadFiles).mockImplementation(
    ((_files: File[], signal?: AbortSignal, onProgress?: (loaded: number, total: number) => void) => {
      captured = signal
      report = onProgress
      return new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(abortError()))
      })
    }) as unknown as typeof api.uploadFiles,
  )
  return { signal: () => captured, progress: (loaded: number, total: number) => report!(loaded, total) }
}

function makeStore(activeSlot: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        // `darwin` so ChatPage's macOS screenshot entry point mounts: the
        // screenshot path shares the `uploading` flag, which is what the
        // screenshot case below is about.
        status: { platform: 'darwin' }, connected: true,
        slots: [{ key: activeSlot, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    } as Partial<RootState>,
  })
}

function renderHost(node: ReactNode, activeSlot: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={makeStore(activeSlot)}>
        <ThemeProvider>
          <MemoryRouter>{node}</MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** Start an upload the ordinary way: pick a file through the hidden input. */
function pickFile(container: HTMLElement, name = 'screencap.mp4', type = 'video/mp4') {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [new File(['x'], name, { type })], configurable: true })
  fireEvent.change(input)
}

/** Paste one image file, the OS-screenshot clipboard shape. A SECOND upload
 *  entry point, and one with no `uploading` gate on it. */
function pasteImage(input: HTMLElement, name = 'pasted.png') {
  const file = new File(['px'], name, { type: 'image/png' })
  fireEvent.paste(input, {
    clipboardData: {
      types: ['Files'],
      items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
      getData: () => '',
    },
  })
}

/** The cancel on the one upload chip a case starts. */
const cancelControl = () => screen.queryByRole('button', { name: /^Cancel uploading / })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
})

describe.each([
  ['ChatPage', (slot: string) => <ChatPage key={slot} />, 'cancel-page'],
  ['ChatPane', (slot: string) => <ChatPane slotKey={slot} />, 'cancel-pane'],
])('%s composer: an upload in flight can be cancelled (#5744)', (_name, node, slot) => {
  it('aborts the request, restores the attach controls, and raises no failure', async () => {
    const { signal } = pendingUpload()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())

    await act(async () => { pickFile(container) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    // The host must hand its own signal down; without one there is nothing to
    // abort and the control below would be decorative.
    await waitFor(() => expect(cancelControl()).toBeInTheDocument())
    expect(signal()).toBeInstanceOf(AbortSignal)
    expect(signal()!.aborted).toBe(false)

    await act(async () => { fireEvent.click(cancelControl()!) })

    expect(signal()!.aborted).toBe(true)
    // The chip goes once its request has ended.
    await waitFor(() => expect(cancelControl()).not.toBeInTheDocument())
    // A cancel the user asked for is not an upload failure.
    expect(screen.queryByText(/max \d+ MB/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Upload failed/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/aborted/i)).not.toBeInTheDocument()
  })

  it('shows the share sent on the chip, and a stall once no byte moves for 20 s', async () => {
    // Only the timeout clock is faked, from the start, so the stall window the
    // upload arms runs on it; vi.waitFor steps it while it polls.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    // The registry is a module singleton: end the upload even when an
    // assertion fails, so the next case does not start beside it.
    onTestFinished(() => { cancelComposerUploads(slot); vi.useRealTimers() })
    const upload = pendingUpload()
    const { container } = renderHost(node(slot), slot)
    await vi.waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container) })
    await vi.waitFor(() => expect(cancelControl()).toBeInTheDocument())
    const stalled = () => screen.queryByText('Stalled')

    // Files still being prepared have sent nothing, and are not stalled.
    act(() => { vi.advanceTimersByTime(UPLOAD_STALL_MS) })
    expect(stalled()).not.toBeInTheDocument()

    // Sent, but never reaching the server: the start is the only report.
    act(() => { upload.progress(0, 1024) })
    expect(screen.getByTestId('composer-upload-progress')).toHaveTextContent('0%')
    act(() => { vi.advanceTimersByTime(UPLOAD_STALL_MS - 1) })
    expect(stalled()).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1) })
    expect(stalled()).toBeInTheDocument()

    // A step of bytes clears the note and shows how much is sent.
    act(() => { upload.progress(256, 1024) })
    expect(stalled()).not.toBeInTheDocument()
    expect(screen.getByTestId('composer-upload-progress')).toHaveTextContent('25%')

    // Every byte sent: the wait is the server's now, not a stall.
    act(() => { upload.progress(1024, 1024) })
    act(() => { vi.advanceTimersByTime(UPLOAD_STALL_MS) })
    expect(stalled()).not.toBeInTheDocument()
    expect(screen.getByTestId('composer-upload-progress')).toHaveTextContent('100%')
  })

  it('shows a stall when upload start reports an unknown total', async () => {
    onTestFinished(() => { cancelComposerUploads(slot); vi.useRealTimers() })
    const upload = pendingUpload()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container) })
    await waitFor(() => expect(cancelControl()).toBeInTheDocument())
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    // XHR loadstart has no known body size yet, even though sending has begun.
    act(() => { upload.progress(0, 0) })
    expect(screen.getByTestId('composer-upload-progress').textContent).toBe('')
    act(() => { vi.advanceTimersByTime(UPLOAD_STALL_MS - 1) })
    expect(screen.queryByText('Stalled')).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByText('Stalled')).toBeInTheDocument()

    await act(async () => { fireEvent.click(cancelControl()!) })
    expect(screen.queryByText('Stalled')).not.toBeInTheDocument()
    expect(cancelControl()).not.toBeInTheDocument()
  })

  it('offers no cancel control when no upload is in flight', async () => {
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    expect(cancelControl()).not.toBeInTheDocument()
  })
})

/**
 * Which request each chip speaks for, at the host level.
 */
describe('ChatPage composer: what each upload chip speaks for (#5744)', () => {
  it('gives a paste beside a pick its own chip, and each cancel ends only its own request', async () => {
    // Paste, drop and a Sketch insert all reach uploadFiles beside a pick, so
    // two requests really can be in flight, and each needs its own way out.
    const signals: AbortSignal[] = []
    vi.mocked(api.uploadFiles).mockImplementation(
      ((_files: File[], signal?: AbortSignal) => {
        if (signal) signals.push(signal)
        return new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(abortError()))
        })
      }) as unknown as typeof api.uploadFiles,
    )
    const { container } = renderHost(<ChatPage key="cancel-multi" />, 'cancel-multi')
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())

    await act(async () => { pickFile(container) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    await act(async () => { pasteImage(screen.getByLabelText('Message input')) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(2))
    expect(signals).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Cancel uploading screencap.mp4' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel uploading pasted.png' })).toBeInTheDocument()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel uploading pasted.png' })) })
    expect(signals.map(s => s.aborted)).toEqual([false, true])

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel uploading screencap.mp4' })) })
    expect(signals.map(s => s.aborted)).toEqual([true, true])
  })

  it('shows no upload chip while a SCREENSHOT holds the same uploading flag', async () => {
    // takeScreenshot sets `uploading` too, but registers no request, so no
    // chip appears during a macOS `screencapture -i`.
    vi.mocked(api.screenshot).mockImplementation(
      (() => new Promise(() => {})) as unknown as typeof api.screenshot,
    )
    renderHost(<ChatPage key="cancel-shot" />, 'cancel-shot')
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())

    await act(async () => { fireEvent.click(screen.getByTitle('Add files & options')) })
    await act(async () => { fireEvent.click(screen.getByText('Screenshot')) })

    // The flag is set, but there is nothing to cancel.
    await waitFor(() => expect(api.screenshot).toHaveBeenCalled())
    expect(cancelControl()).not.toBeInTheDocument()
    expect(api.uploadFiles).not.toHaveBeenCalled()
  })
})

/**
 * A large set waits its turn (UPLOAD_CONCURRENCY in flight). Past six requests
 * the browser queues the rest itself, and a request it holds back reports its
 * start at zero bytes, so its chip would read Stalled though nothing is wrong.
 */
describe.each([
  ['ChatPage', (slot: string) => <ChatPage key={slot} />, 'queue-page'],
  ['ChatPane', (slot: string) => <ChatPane slotKey={slot} />, 'queue-pane'],
])('%s composer: a set of files shares a limit of requests in flight', (_name, node, base) => {
  /** Each request the host started, by file name: it reports its start at
   *  zero bytes, as XHR loadstart does, and settles when the case says. */
  function controlledUploads() {
    const started = new Map<string, { signal: AbortSignal; land: () => void }>()
    let inFlight = 0
    let peak = 0
    vi.mocked(api.uploadFiles).mockImplementation(
      ((files: File[], signal?: AbortSignal, onProgress?: (loaded: number, total: number) => void) => {
        const name = files[0].name
        inFlight++
        peak = Math.max(peak, inFlight)
        onProgress?.(0, 1024)
        return new Promise((resolve, reject) => {
          const done = () => { inFlight-- }
          signal?.addEventListener('abort', () => { done(); reject(abortError()) })
          started.set(name, { signal: signal!, land: () => { done(); resolve({ paths: [`/up/${name}`] }) } })
        })
      }) as unknown as typeof api.uploadFiles,
    )
    return { started, peak: () => peak }
  }

  function pickFiles(container: HTMLElement, names: string[]) {
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: names.map(n => new File(['x'], n, { type: 'text/plain' })), configurable: true })
    fireEvent.change(input)
  }

  const NAMES = ['f1.txt', 'f2.txt', 'f3.txt', 'f4.txt', 'f5.txt']

  it('keeps at most 3 requests in flight, gives every file its chip at once, and lands them all', async () => {
    // Its own slot: a pane keeps the files that landed for a slot.
    const slot = `${base}-all`
    onTestFinished(() => { cancelComposerUploads(slot) })
    const uploads = controlledUploads()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFiles(container, NAMES) })

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(3))
    expect(screen.getAllByTestId('composer-upload-chip')).toHaveLength(5)
    expect([...uploads.started.keys()]).toEqual(['f1.txt', 'f2.txt', 'f3.txt'])

    await act(async () => { uploads.started.get('f1.txt')!.land() })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(4))
    await act(async () => { uploads.started.get('f2.txt')!.land(); uploads.started.get('f3.txt')!.land() })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(5))
    await act(async () => { uploads.started.get('f4.txt')!.land(); uploads.started.get('f5.txt')!.land() })

    await waitFor(() => expect(screen.queryAllByTestId('composer-upload-chip')).toHaveLength(0))
    expect(uploads.peak()).toBe(3)
    for (const name of NAMES) expect(screen.getByRole('group', { name: `/up/${name}` })).toBeInTheDocument()
  })

  it('never shows Stalled on a file still waiting its turn', async () => {
    const slot = `${base}-wait`
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    onTestFinished(() => { cancelComposerUploads(slot); vi.useRealTimers() })
    controlledUploads()
    const { container } = renderHost(node(slot), slot)
    await vi.waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFiles(container, NAMES) })
    await vi.waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(3))

    act(() => { vi.advanceTimersByTime(UPLOAD_STALL_MS) })
    const chip = (name: string) => screen.getByRole('group', { name })
    // The three on the wire reported their start and moved no byte since.
    for (const name of ['f1.txt', 'f2.txt', 'f3.txt']) expect(chip(name)).toHaveTextContent('Stalled')
    // The two waiting have sent nothing: no percent, no stall.
    for (const name of ['f4.txt', 'f5.txt']) {
      expect(chip(name)).not.toHaveTextContent('Stalled')
      expect(chip(name)).not.toHaveTextContent('%')
    }
  })

  it('drops a cancelled waiting file without ever sending it, and the rest still land', async () => {
    const slot = `${base}-cancel`
    onTestFinished(() => { cancelComposerUploads(slot) })
    const uploads = controlledUploads()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFiles(container, NAMES) })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(3))

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel uploading f4.txt' })) })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel uploading f4.txt' })).not.toBeInTheDocument())
    expect(screen.getAllByTestId('composer-upload-chip')).toHaveLength(4)

    await act(async () => { for (const name of ['f1.txt', 'f2.txt', 'f3.txt']) uploads.started.get(name)!.land() })
    await waitFor(() => expect(uploads.started.has('f5.txt')).toBe(true))
    await act(async () => { uploads.started.get('f5.txt')!.land() })

    await waitFor(() => expect(screen.queryAllByTestId('composer-upload-chip')).toHaveLength(0))
    expect(api.uploadFiles).toHaveBeenCalledTimes(4)
    expect(uploads.started.has('f4.txt')).toBe(false)
    expect(screen.queryByRole('group', { name: '/up/f4.txt' })).not.toBeInTheDocument()
    for (const name of ['f1.txt', 'f2.txt', 'f3.txt', 'f5.txt']) expect(screen.getByRole('group', { name: `/up/${name}` })).toBeInTheDocument()
    // No failure for the cancel, and Send is free again: every hold released.
    expect(screen.queryByText(/Upload failed/i)).not.toBeInTheDocument()
    expect(isComposerSendHeld(slot)).toBe(false)
  })
})
