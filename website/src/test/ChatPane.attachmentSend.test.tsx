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

/* ChatPane must serialize attachments exactly as ChatPage does
 * (prepareSendPayload): an image becomes a producer-form `![image](dest)` line
 * on BOTH the wire text and the optimistic bubble, a non-image file an
 * `[attached_file N] path` marker on the wire with the ordered non-image list
 * on `meta.files`. Before this the pane shipped the typed text verbatim and
 * parked every path on `meta.files` alone -- so a picture attached in a member
 * DM or a split pane neither rendered in the bubble (images render only from
 * their markdown) nor reached the agent (whose image extraction matches
 * absolute paths in the PROMPT TEXT), while the same send from the main chat
 * did both. */

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
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span data-testid="markdown">{content}</span> }))
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
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

function userRow(store: ReturnType<typeof makeStore>, slot: string) {
  return store.getState().chat.slotMessages[slot]?.find((m) => m.role === 'user')
}

async function stageUpload(container: HTMLElement, name: string, type: string) {
  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement
  const file = new File(['x'], name, { type })
  Object.defineProperty(fileInput, 'files', { value: [file] })
  fireEvent.change(fileInput)
  await waitFor(() => expect(api.uploadFiles).toHaveBeenCalled())
}

async function sendText(text: string) {
  const box = (await screen.findAllByRole('textbox'))[0]
  if (text) fireEvent.change(box, { target: { value: text } })
  fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
}

function lastSend() {
  const calls = (api.sendChat as ReturnType<typeof vi.fn>).mock.calls
  const [wireText, , , , meta] = calls[calls.length - 1]
  return { wireText: wireText as string, meta: meta as Record<string, unknown> | undefined }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane send — attachment serialization (parity with ChatPage)', () => {
  it('ships an attached image as its markdown line on the wire AND in the bubble, not on meta.files', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/home/u/.kiro/crew/uploads/shot.png'] })
    const { store, container } = renderPane('pane-img')
    await stageUpload(container, 'shot.png', 'image/png')
    await sendText('what is wrong here?')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const { wireText, meta } = lastSend()
    // The agent's image extraction matches an absolute path in the prompt
    // TEXT: this line is what turns the upload into a real image block.
    expect(wireText).toBe('![image](/home/u/.kiro/crew/uploads/shot.png)\n\nwhat is wrong here?')
    // Images never ride meta.files (prepareSendPayload keeps it image-free);
    // the row's ONE image source of truth is the markdown line.
    expect(meta).toEqual({ sendId: expect.stringMatching(/^s-/) })
    // The optimistic bubble carries the same markdown, so the picture renders
    // the moment it is sent -- the main chat's contract.
    const row = userRow(store, 'pane-img')
    expect(row?.content).toBe('![image](/home/u/.kiro/crew/uploads/shot.png)\n\nwhat is wrong here?')
    expect(row?.meta?.files).toBeUndefined()
  })

  it('ships a non-image file as an [attached_file N] marker with the ordered path on meta.files', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/report.pdf'] })
    const { store, container } = renderPane('pane-doc')
    await stageUpload(container, 'report.pdf', 'application/pdf')
    await sendText('summarize this')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const { wireText, meta } = lastSend()
    expect(wireText).toBe('summarize this\n[attached_file 1] /tmp/report.pdf')
    expect(meta).toEqual({ files: ['/tmp/report.pdf'], sendId: expect.stringMatching(/^s-/) })
    // The bubble keeps the typed text; the card is drawn from meta.files.
    const row = userRow(store, 'pane-doc')
    expect(row?.content).toBe('summarize this')
    expect(row?.meta?.files).toEqual(['/tmp/report.pdf'])
  })

  it('keeps the image out of meta.files and the file in it on a mixed send', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/a.png', '/tmp/notes.txt'] })
    const { container } = renderPane('pane-mixed')
    await stageUpload(container, 'a.png', 'image/png')
    await sendText('both')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const { wireText, meta } = lastSend()
    expect(wireText).toBe('![image](/tmp/a.png)\n\nboth\n[attached_file 1] /tmp/notes.txt')
    // Token 1 indexes meta.files[0]: the list is the image-FILTERED order, the
    // same list ChatPage persists, so a history replay resolves the marker.
    expect(meta).toEqual({ files: ['/tmp/notes.txt'], sendId: expect.stringMatching(/^s-/) })
  })

  it('an image-only send has a non-empty wire text, so the server no longer refuses it', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/only.png'] })
    const { container } = renderPane('pane-imgonly')
    await stageUpload(container, 'only.png', 'image/png')
    await sendText('')

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // Before: '' -> 400 message_required and the picture was lost. The image
    // line IS the message now, exactly as the main chat has always sent it.
    expect(lastSend().wireText).toBe('![image](/tmp/only.png)')
  })
})
