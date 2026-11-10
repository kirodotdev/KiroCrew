import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, sseChatMessage } from '../store/chatSlice'
import type { SendTurnOptions } from '../chat-core/transport/sendTurn'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* The dictation note on the split-pane / crewmate DM composer's send and steer
 * paths (`useChatPaneSend`). `markDictated` stands in for the Voice atom, whose
 * own recording of provenance is pinned in useComposerVoiceDictationNote.test. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: true, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) }),
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
    editQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    interruptSlot: vi.fn().mockResolvedValue({ ok: true }),
    reorderQueuedMessages: vi.fn().mockResolvedValue({ ok: true }),
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
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

let lastSendTurnOpts: SendTurnOptions | null = null
vi.mock('../chat-core/transport/sendTurn', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/transport/sendTurn')>()
  return {
    ...actual,
    sendTurn: (opts: SendTurnOptions) => {
      lastSendTurnOpts = opts
      return actual.sendTurn(opts)
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { queryClient } from '../api/queryClient'
import { DICTATION_NOTE, PANE_DICTATION_SCOPE, _resetDictationProvenance, isDictated, markDictated } from '../chat-core/composer/dictationProvenance'

// The pane keeps its own record (its draft is its own), so these mark and read
// under the pane's scope, as its Voice atom does.
const markPane = (slot: string) => markDictated(slot, PANE_DICTATION_SCOPE)
const isPaneDictated = (slot: string) => isDictated(slot, PANE_DICTATION_SCOPE)

function makeStore(slotKey: string, running: boolean) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running, mode: 'member', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  store.dispatch(setActiveSlot('front'))
  if (running) store.dispatch(sseChatMessage({ slot: slotKey, role: 'chunk', content: 'working…', seq: 1 }))
  return store
}

function renderPane(slotKey: string, opts: { running: boolean }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey, opts.running)
  const ui = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={key} busyMode="steer-only" />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const r = render(ui(slotKey))
  const rebind = (key: string) => r.rerender(ui(key))
  return Object.assign(r, { store, rebind })
}

const composer = async () => (await screen.findAllByRole('textbox'))[0] as HTMLTextAreaElement

function send(box: HTMLTextAreaElement, text: string) {
  fireEvent.change(box, { target: { value: text } })
  fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPaneDraftsForTests()
  _resetDictationProvenance()
  lastSendTurnOpts = null
  queryClient.setQueryData(['sttConfig'], { enabled: true, available: true, provider: 'local', dictation_note: true })
  vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, steered: true }) } as unknown as Response)
  vi.mocked(api.chatSlotDetail).mockResolvedValue({ messages: [], running: true, has_more: false, total: 0 } as never)
})

describe('ChatPane dictation note', () => {
  it('an idle send of dictated text ends with the note; the next typed send does not', async () => {
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as unknown as Response)
    renderPane('member-idle', { running: false })
    const box = await composer()
    markPane('member-idle')

    send(box, 'call me back')
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.message).toBe(`call me back\n\n${DICTATION_NOTE}`)
    expect(lastSendTurnOpts!.steer).toBeFalsy()

    lastSendTurnOpts = null
    send(box, 'typed follow-up')
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.message).toBe('typed follow-up')
  })

  it('emptying the composer forgets the dictation, so a fresh typed message gets no note', async () => {
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as unknown as Response)
    renderPane('member-empty', { running: false })
    const box = await composer()
    markPane('member-empty')
    fireEvent.change(box, { target: { value: 'spoken then deleted' } })
    fireEvent.change(box, { target: { value: '' } })
    expect(isPaneDictated('member-empty')).toBe(false)

    send(box, 'typed instead')
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.message).toBe('typed instead')
  })

  it('a steer into a running turn carries the note at the end of its one text', async () => {
    renderPane('member-steer', { running: true })
    const box = await composer()
    markPane('member-steer')

    send(box, 'actually use the other file')

    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.steer).toBe(true)
    expect(lastSendTurnOpts!.message).toBe(`actually use the other file\n\n${DICTATION_NOTE}`)
  })

  it('a refused steer hands the typed words back without the note, and the retry carries it once', async () => {
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, status: 409, json: () => Promise.resolve({ error: 'busy' }) } as unknown as Response)
    renderPane('member-refused', { running: true })
    const box = await composer()
    markPane('member-refused')

    send(box, 'say it again')
    await waitFor(() => expect(box.value).toBe('say it again'))
    expect(isPaneDictated('member-refused')).toBe(true)

    lastSendTurnOpts = null
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.message).toBe(`say it again\n\n${DICTATION_NOTE}`)
  })

  it('a steer demoted to the queue stashes its provenance, so cancelling the card can put it back', async () => {
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q-dictated' }) } as unknown as Response)
    renderPane('member-demoted', { running: true })
    const box = await composer()
    markPane('member-demoted')

    send(box, 'park this one')

    await waitFor(() => expect(queuedSendStash.get('q-dictated')).toEqual({
      raw: 'park this one', files: [], sent: `park this one\n\n${DICTATION_NOTE}`, dictated: true,
    }))
    queuedSendStash.delete('q-dictated')
  })

  it('words dictated into the main chat for the same session do not mark a message typed in the pane', async () => {
    // The page and the pane keep separate drafts for one session, so a mark
    // the page's composer holds is not the pane's.
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as unknown as Response)
    renderPane('member-shared', { running: false })
    const box = await composer()
    markDictated('member-shared')

    send(box, 'typed in the pane')

    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.message).toBe('typed in the pane')
    expect(isDictated('member-shared')).toBe(true)
  })

  it('rebinding the pane to another member does not carry the mark there', async () => {
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as unknown as Response)
    const { rebind } = renderPane('member-a', { running: false })
    await composer()
    markPane('member-a')

    rebind('member-b')
    const box = await composer()
    send(box, 'typed for b')

    await waitFor(() => expect(lastSendTurnOpts).not.toBeNull())
    expect(lastSendTurnOpts!.message).toBe('typed for b')
    expect(isPaneDictated('member-a')).toBe(true)
  })
})
