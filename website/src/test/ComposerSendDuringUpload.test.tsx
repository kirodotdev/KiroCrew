/**
 * Send while a composer upload is still in flight.
 *
 * Each host holds every send path while an upload is on the wire. Completed
 * paths enter the originating slot's arrivals inbox before the final hold is
 * released, so whichever composer shows that slot receives the attachment.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { useLayoutEffect } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
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
    dashboardConfig: vi.fn().mockResolvedValue({ session_grid: true }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error { status = 0; body = '' },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
// Seam on the composer's Voice atom: captures each host's auto-submit callback
// (what the endpointer fires) and records `disarmForSend` calls, over the real
// hook so the composer otherwise behaves as in the other tests here.
const voiceSeam = vi.hoisted(() => ({
  autoSubmit: {} as Record<string, (() => void) | undefined>,
  submitOnLayoutFor: undefined as string | undefined,
  disarmForSend: vi.fn(),
}))
vi.mock('../chat-core/composer/useComposerVoice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../chat-core/composer/useComposerVoice')>()
  return {
    ...actual,
    useComposerVoice: (host: Parameters<typeof actual.useComposerVoice>[0]) => {
      const { sessionId, onAutoSubmit } = host
      voiceSeam.autoSubmit[sessionId ?? ''] = onAutoSubmit
      useLayoutEffect(() => {
        if (voiceSeam.submitOnLayoutFor !== sessionId) return
        voiceSeam.submitOnLayoutFor = undefined
        void onAutoSubmit()
      }, [sessionId, onAutoSubmit])
      const cv = actual.useComposerVoice(host)
      return { ...cv, disarmForSend: () => { voiceSeam.disarmForSend(); cv.disarmForSend() } }
    },
  }
})
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
// Seam on the snip+crop path: support is off by default (jsdom has no
// getDisplayMedia, so the composer's Screenshot item takes the macOS route as
// in the other tests here); a test flips it on and settles the capture by
// hand. Canvas pixel ops cannot run in jsdom, so crop/encode are stubbed.
const snipSeam = vi.hoisted(() => ({
  supported: false,
  captures: [] as Array<(frame: HTMLCanvasElement | null) => void>,
  file: new File(['px'], 'snip-1.png', { type: 'image/png' }),
}))
vi.mock('../hooks/useScreenSnip', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useScreenSnip')>()
  return {
    ...actual,
    isScreenSnipSupported: () => snipSeam.supported,
    get screenSnipSupported() { return snipSeam.supported },
    captureScreen: () => new Promise<HTMLCanvasElement | null>(r => { snipSeam.captures.push(r) }),
    cropCanvas: () => ({}) as HTMLCanvasElement,
    canvasToFile: async () => snipSeam.file,
  }
})
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/SessionGridView', () => ({ default: () => <div data-testid="session-grid" /> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) })

import ChatPage from '../pages/ChatPage'
import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { __resetPaneDraftsForTests, readPaneDraft } from '../utils/chatPaneDrafts'
import { holdComposerSend, isComposerSendHeld, releaseComposerSend } from '../utils/composerSendHolds'
import { PASTE_DRAFTS_KEY } from '../utils/chatPasteDrafts'

function makeStore(activeSlot: string, platform = 'linux') {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: { status: { platform }, connected: true,
        slots: [activeSlot, 'other-slot'].map(key => ({ key, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal', subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: { activeSlot, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle', history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [], historyOffset: 0, _wsChunkedDuringFetch: false, slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    } as Partial<RootState>,
  })
}

type UploadResult = { paths: string[] }

/** Each call to api.uploadFiles returns a promise the test settles by hand. */
function manualUploads(): Array<(r: UploadResult) => void> {
  const resolvers: Array<(r: UploadResult) => void> = []
  vi.mocked(api.uploadFiles).mockImplementation(
    (() => new Promise<UploadResult>(r => { resolvers.push(r) })) as unknown as typeof api.uploadFiles,
  )
  return resolvers
}

function renderHost(node: ReactNode, slot: string, platform?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const store = makeStore(slot, platform)
  const tree = (content: ReactNode) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}><ThemeProvider><MemoryRouter>{content}</MemoryRouter></ThemeProvider></Provider>
    </QueryClientProvider>
  )
  const view = render(tree(node))
  return { store, ...view, rerenderHost: (content: ReactNode) => view.rerender(tree(content)) }
}

function pickFile(container: HTMLElement, name: string) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [new File(['x'], name, { type: 'text/plain' })], configurable: true })
  fireEvent.change(input)
}

function pasteImage(target: HTMLElement, name: string) {
  const file = new File(['px'], name, { type: 'image/png' })
  fireEvent.paste(target, {
    clipboardData: { types: ['Files'], items: [{ kind: 'file', type: file.type, getAsFile: () => file }], getData: () => '' },
  })
}

const sendButton = () => screen.getAllByRole('button', { name: 'Send' })[0] as HTMLButtonElement
const sentPayloads = () => JSON.stringify(vi.mocked(api.sendChat).mock.calls)

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
})

describe.each([
  ['ChatPage', (s: string) => <ChatPage key={s} />, 'send-upload-page'],
  ['ChatPane', (s: string) => <ChatPane slotKey={s} />, 'send-upload-pane'],
])('%s composer: Send waits for an upload in flight', (_name, node, slot) => {
  it('holds Send and Enter until the upload lands, then sends WITH the file', async () => {
    const uploads = manualUploads()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container, 'notes.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    const box = screen.getByLabelText('Message input')
    await act(async () => { fireEvent.change(box, { target: { value: 'look at this' } }) })

    expect(sendButton()).toBeDisabled()
    await act(async () => { fireEvent.click(sendButton()) })
    await act(async () => { fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' }) })
    expect(api.sendChat).not.toHaveBeenCalled()

    await act(async () => { uploads[0]({ paths: ['/up/notes.txt'] }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendButton()) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentPayloads()).toContain('/up/notes.txt')
    expect(sentPayloads()).toContain('look at this')
  })

  it('keeps Send held until the LAST of two concurrent uploads settles', async () => {
    const uploads = manualUploads()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container, 'first.txt') })
    await act(async () => { pasteImage(screen.getByLabelText('Message input'), 'second.png') })
    await waitFor(() => expect(uploads).toHaveLength(2))
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'both' } }) })

    // The LATER request settles first: a flag that tracked only the latest
    // request (or cleared on the first settle) would open Send here.
    await act(async () => { uploads[1]({ paths: ['/up/second.png'] }) })
    expect(sendButton()).toBeDisabled()

    await act(async () => { uploads[0]({ paths: ['/up/first.txt'] }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
  })

  it('keeps the cancel control up while a sibling upload still holds Send', async () => {
    const uploads = manualUploads()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container, 'first.txt') })
    await act(async () => { pasteImage(screen.getByLabelText('Message input'), 'second.png') })
    await waitFor(() => expect(uploads).toHaveLength(2))
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'both' } }) })
    expect(screen.getByRole('button', { name: 'Cancel upload' })).toBeInTheDocument()

    // The LATER request settles first. Send is still held for the first one,
    // so the user must still be able to see and abort it: a host feeding
    // ChatInput's `uploading` from a single shared flag drops both here.
    await act(async () => { uploads[1]({ paths: ['/up/second.png'] }) })
    expect(sendButton()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel upload' })).toBeInTheDocument()

    await act(async () => { uploads[0]({ paths: ['/up/first.txt'] }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    expect(screen.queryByRole('button', { name: 'Cancel upload' })).toBeNull()
  })
})

describe.each([
  ['ChatPage', (s: string) => <ChatPage key={s} />, 'held-voice-page'],
  ['ChatPane', (s: string) => <ChatPane slotKey={s} />, 'held-voice-pane'],
])('%s composer: a held voice auto-submit still ends the dictation', (_name, node, slot) => {
  it('disarms the voice capture and sends nothing while the upload is on the wire', async () => {
    // The endpointer's auto-submit reaches send() directly, past ChatInput's
    // own hold. The hold must still refuse the send, but the capture has to be
    // disarmed anyway: `useComposerVoice.onEndpoint` relies on the host's send
    // to stop it, and nothing re-fires when the hold clears, so a capture left
    // armed keeps appending later speech to the unsent draft.
    const uploads = manualUploads()
    const { container } = renderHost(node(slot), slot)
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container, 'notes.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'dictated so far' } }) })
    await waitFor(() => expect(voiceSeam.autoSubmit[slot]).toBeTypeOf('function'))
    voiceSeam.disarmForSend.mockClear()

    await act(async () => { voiceSeam.autoSubmit[slot]!() })

    expect(api.sendChat).not.toHaveBeenCalled()
    expect(voiceSeam.disarmForSend).toHaveBeenCalledTimes(1)
    // The hold keeps the draft: the text stays for the send that follows.
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('dictated so far')
  })
})

describe('ChatPage composer: which sends an attachment holds', () => {
  it('holds only the slot the upload lands in, not the slot switched to', async () => {
    const uploads = manualUploads()
    const { container, store } = renderHost(<ChatPage key="hold-a" />, 'hold-a')
    await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(container, 'for-a.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'in a' } }) })
    expect(sendButton()).toBeDisabled()

    // The file will land in hold-a's draft, so other-slot's send is not at risk.
    await act(async () => { store.dispatch(setActiveSlot('other-slot')) })
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'in other' } }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
  })

  it('holds the outgoing composer during the active-slot render/effect gap', async () => {
    const { store } = renderHost(<ChatPage key="switch-gap" />, 'hold-a')
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'draft owned by a' } }) })
    await waitFor(() => expect(voiceSeam.autoSubmit['hold-a']).toBeTypeOf('function'))

    holdComposerSend('hold-a')
    try {
      // The voice endpoint runs in the incoming composer's layout effect: B is
      // committed into activeSlotRef, but ChatPage's passive effect has not yet
      // advanced composerSlotRef from A. This is the one-commit ownership gap.
      voiceSeam.submitOnLayoutFor = 'other-slot'
      await act(async () => { store.dispatch(setActiveSlot('other-slot')) })

      expect(api.sendChat).not.toHaveBeenCalled()
      expect(voiceSeam.disarmForSend).toHaveBeenCalledTimes(1)
    } finally {
      voiceSeam.submitOnLayoutFor = undefined
      releaseComposerSend('hold-a')
    }
  })

  it('queues a late upload until ChatPage shows the slot after split view', async () => {
    __resetPaneDraftsForTests()
    const slot = 'split-upload-page'
    const uploads = manualUploads()
    const view = renderHost(<ChatPage key={slot} />, slot)
    await waitFor(() => expect(view.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(view.container, 'late.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Enter split view' })) })
    await waitFor(() => expect(screen.getByTestId('session-grid')).toBeInTheDocument())
    await act(async () => { uploads[0]({ paths: ['/up/late.txt'] }) })
    expect(screen.queryByText('late.txt')).toBeNull()
    expect(readPaneDraft(slot).files).toEqual([])
    expect(isComposerSendHeld(slot)).toBe(false)

    view.rerenderHost(<ChatPage key={`${slot}-return`} />)
    await waitFor(() => expect(screen.getByText('late.txt')).toBeInTheDocument())
    expect(sendButton()).not.toBeDisabled()
  })

  it('keeps Send held for a screenshot still capturing after a paste upload settles', async () => {
    // Uploads and the macOS screenshot share the `uploading` flag, which the
    // first to finish clears; the send hold is counted per producer instead.
    let finishShot: (r: { path: string }) => void = () => {}
    vi.mocked(api.screenshot).mockImplementation(
      (() => new Promise(r => { finishShot = r })) as unknown as typeof api.screenshot,
    )
    const uploads = manualUploads()
    renderHost(<ChatPage key="hold-shot" />, 'hold-shot', 'darwin')
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByTitle('Add files & options')) })
    await act(async () => { fireEvent.click(screen.getByText('Screenshot')) })
    await waitFor(() => expect(api.screenshot).toHaveBeenCalled())
    await act(async () => { pasteImage(screen.getByLabelText('Message input'), 'pasted.png') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'shot' } }) })

    await act(async () => { uploads[0]({ paths: ['/up/pasted.png'] }) })
    expect(sendButton()).toBeDisabled()

    await act(async () => { finishShot({ path: '/shots/cap.png' }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
  })
})

describe('ChatPage composer: upload hold survives a remount', () => {
  it('hands a late upload to the fresh ChatPage before releasing Send', async () => {
    __resetPaneDraftsForTests()
    const uploads = manualUploads()
    const first = renderHost(<ChatPage key="remount-page-first" />, 'remount-page')
    await waitFor(() => expect(first.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(first.container, 'pending.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    first.unmount()

    const second = renderHost(<ChatPage key="remount-page-second" />, 'remount-page')
    await waitFor(() => expect(second.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'after reopen' } }) })
    expect(sendButton()).toBeDisabled()

    await act(async () => { uploads[0]({ paths: ['/up/pending.txt'] }) })
    await waitFor(() => expect(screen.getByText('pending.txt')).toBeInTheDocument())
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendButton()) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentPayloads()).toContain('/up/pending.txt')
    expect(sentPayloads()).toContain('after reopen')
  })
})

describe('ChatPage composer: a completed upload survives an empty host interval', () => {
  it('drains the attachment after upload settles with ChatPage unmounted', async () => {
    const slot = 'unmounted-settle-page'
    const uploads = manualUploads()
    const first = renderHost(<ChatPage key={`${slot}-first`} />, slot)
    await waitFor(() => expect(first.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(first.container, 'arrived-away.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    first.unmount()

    await act(async () => { uploads[0]({ paths: ['/up/arrived-away.txt'] }) })
    expect(isComposerSendHeld(slot)).toBe(false)

    renderHost(<ChatPage key={`${slot}-return`} />, slot)
    await waitFor(() => expect(screen.getByText('arrived-away.txt')).toBeInTheDocument())
    expect(sendButton()).not.toBeDisabled()
  })

  it('keeps a file for its own session after a switch and leaving the chat page', async () => {
    const slot = 'switched-away-page'
    const uploads = manualUploads()
    const first = renderHost(<ChatPage key={`${slot}-first`} />, slot)
    await waitFor(() => expect(first.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(first.container, 'switched-away.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    await act(async () => { first.store.dispatch(setActiveSlot('other-session')) })
    first.unmount()

    await act(async () => { uploads[0]({ paths: ['/up/switched-away.txt'] }) })
    expect(isComposerSendHeld(slot)).toBe(false)

    // Back on the other session: the file waits for its own session.
    const other = renderHost(<ChatPage key={`${slot}-other`} />, 'other-session')
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    expect(screen.queryByText('switched-away.txt')).toBeNull()
    other.unmount()

    renderHost(<ChatPage key={`${slot}-return`} />, slot)
    await waitFor(() => expect(screen.getByText('switched-away.txt')).toBeInTheDocument())
  })
})

describe('ChatPane composer: upload hold survives a remount', () => {
  it('hands a late upload to ChatPage when split view collapses', async () => {
    const uploads = manualUploads()
    const view = renderHost(<ChatPane slotKey="collapse-upload" />, 'collapse-upload')
    await waitFor(() => expect(view.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(view.container, 'after-collapse.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))

    view.rerenderHost(<ChatPage key="collapse-upload" />)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'collapsed' } }) })
    expect(sendButton()).toBeDisabled()

    await act(async () => { uploads[0]({ paths: ['/up/after-collapse.txt'] }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendButton()) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentPayloads()).toContain('/up/after-collapse.txt')
    expect(sentPayloads()).toContain('collapsed')
  })

  it('leaves a parked pane text draft in the pane store when split view collapses', async () => {
    // Only the late FILES cross to ChatPage. A pane's text (and the paste
    // blocks behind its tokens) stays parked for the next pane that shows the
    // slot: ChatPage has its own text and paste drafts and must not fold a
    // pane's into them.
    __resetPaneDraftsForTests()
    const view = renderHost(<ChatPane slotKey="collapse-text" />, 'collapse-text')
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'typed in the pane' } }) })

    view.rerenderHost(<ChatPage key="collapse-text" />)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    // Give the absorb effect a tick so an (incorrect) text take would show.
    await act(async () => { await Promise.resolve() })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
    expect(readPaneDraft('collapse-text').text).toBe('typed in the pane')
    // ChatPage's own paste blocks were not rewritten by the pane draft.
    expect(JSON.parse(localStorage.getItem(PASTE_DRAFTS_KEY) ?? '{}')['collapse-text']).toBeUndefined()

    // A remounted pane picks the parked text back up.
    view.rerenderHost(<ChatPane slotKey="collapse-text" />)
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('typed in the pane'))
  })

  it('leaves an ordinary pane draft with its file parked whole when split view collapses', async () => {
    // No upload in flight: the pane parks its whole composer, text AND an
    // attachment that already landed. ChatPage absorbs only late arrivals
    // (the slot's send is still held), so this draft stays parked as one
    // unit for the next pane instead of its file being attached to ChatPage's
    // own draft while the text it belonged with stays behind.
    __resetPaneDraftsForTests()
    const uploads = manualUploads()
    const view = renderHost(<ChatPane slotKey="collapse-whole" />, 'collapse-whole')
    await waitFor(() => expect(view.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'goes with the file' } }) })
    await act(async () => { pickFile(view.container, 'landed.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    await act(async () => { uploads[0]({ paths: ['/up/landed.txt'] }) })
    await waitFor(() => expect(screen.getByText('landed.txt')).toBeInTheDocument())
    expect(isComposerSendHeld('collapse-whole')).toBe(false)

    view.rerenderHost(<ChatPage key="collapse-whole" />)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByText('landed.txt')).toBeNull()
    expect(readPaneDraft('collapse-whole')).toMatchObject({ text: 'goes with the file', files: ['/up/landed.txt'] })

    // The next pane showing the slot gets text and file back together.
    view.rerenderHost(<ChatPane slotKey="collapse-whole" />)
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('goes with the file'))
    expect(screen.getByText('landed.txt')).toBeInTheDocument()
  })

  it('keeps Send disabled in a fresh pane until the pending upload settles', async () => {
    const uploads = manualUploads()
    const first = renderHost(<ChatPane slotKey="remount-pane" />, 'remount-pane')
    await waitFor(() => expect(first.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(first.container, 'pending.txt') })
    await waitFor(() => expect(uploads).toHaveLength(1))
    first.unmount()

    const second = renderHost(<ChatPane slotKey="remount-pane" />, 'remount-pane')
    await waitFor(() => expect(second.container.querySelector('input[type="file"]')).toBeTruthy())
    // A typed draft, so a disabled Send can only mean the hold survived.
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'after reopen' } }) })
    expect(sendButton()).toBeDisabled()
    expect(screen.getByTestId('composer-send-held')).toBeInTheDocument()

    await act(async () => { uploads[0]({ paths: ['/up/pending.txt'] }) })
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
  })

  it('lets the fresh pane cancel the request the old pane started', async () => {
    // The hold outlives the pane, so the cancel handle must too: a request
    // that hangs (api.uploadFiles sets no timeout) would otherwise leave the
    // reopened pane held with no Cancel and its Send dead until reload.
    let signal: AbortSignal | undefined
    vi.mocked(api.uploadFiles).mockImplementation(
      ((_files: File[], s?: AbortSignal) => {
        signal = s
        return new Promise((_resolve, reject) => {
          s?.addEventListener('abort', () => reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' })))
        })
      }) as unknown as typeof api.uploadFiles,
    )
    const first = renderHost(<ChatPane slotKey="remount-cancel" />, 'remount-cancel')
    await waitFor(() => expect(first.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { pickFile(first.container, 'stuck.txt') })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    expect(signal).toBeInstanceOf(AbortSignal)
    first.unmount()

    const second = renderHost(<ChatPane slotKey="remount-cancel" />, 'remount-cancel')
    await waitFor(() => expect(second.container.querySelector('input[type="file"]')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'after reopen' } }) })
    expect(sendButton()).toBeDisabled()
    // The reopened pane did not start the request, but it is where the user is.
    const cancel = screen.getByRole('button', { name: 'Cancel upload' })

    await act(async () => { fireEvent.click(cancel) })

    expect(signal!.aborted).toBe(true)
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    expect(screen.queryByRole('button', { name: 'Cancel upload' })).toBeNull()
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('after reopen')
    // A cancel the user asked for is not a failure.
    expect(screen.queryByText(/Upload failed/i)).toBeNull()
  })
})

describe('ChatPage composer: Send waits for a snip in progress', () => {
  // The snip has three stretches with no upload yet: the browser's share
  // prompt (captureScreen pending), the crop overlay, and the hand-off into
  // uploadFiles. A voice auto-submit in any of them used to send without the
  // file, so the snip holds the slot's Send from the click to the upload's
  // own hold, and releases exactly once however it ends.
  const fakeFrame = { width: 400, height: 200, toDataURL: () => 'data:image/png;base64,AAAA' } as unknown as HTMLCanvasElement

  beforeEach(() => { snipSeam.supported = true; snipSeam.captures.length = 0 })
  afterEach(() => { snipSeam.supported = false })

  async function startSnip(slot: string) {
    const uploads = manualUploads()
    renderHost(<ChatPage key={slot} />, slot)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'while snipping' } }) })
    await act(async () => { fireEvent.click(screen.getByTitle('Add files & options')) })
    await act(async () => { fireEvent.click(screen.getByText('Screenshot')) })
    await waitFor(() => expect(snipSeam.captures).toHaveLength(1))
    return uploads
  }

  /** Each event is its own act (RTL wraps fireEvent): the overlay's mousemove
   *  reads the `start` state the mousedown set, so they cannot be batched. */
  function drag(surface: HTMLElement) {
    surface.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 100, right: 200, bottom: 100, x: 0, y: 0, toJSON() {} }) as DOMRect
    fireEvent.mouseDown(surface, { clientX: 10, clientY: 10 })
    fireEvent.mouseMove(surface, { clientX: 90, clientY: 60 })
    fireEvent.mouseUp(surface, { clientX: 90, clientY: 60 })
  }

  it('holds Send from the share prompt through the crop and hands off to the upload hold', async () => {
    const slot = 'snip-complete'
    const uploads = await startSnip(slot)
    // Share prompt open: held, and a voice auto-submit sends nothing.
    expect(isComposerSendHeld(slot)).toBe(true)
    expect(sendButton()).toBeDisabled()
    await act(async () => { await voiceSeam.autoSubmit[slot]?.() })
    expect(api.sendChat).not.toHaveBeenCalled()

    await act(async () => { snipSeam.captures[0](fakeFrame) })
    const surface = await screen.findByTestId('snip-surface')
    expect(isComposerSendHeld(slot)).toBe(true)

    // The upload's request starts while the slot is still held: the crop
    // hands off with the snip hold still up, and uploadFiles adds its own
    // before the snip's is released.
    let heldWhenUploadStarted: boolean | undefined
    vi.mocked(api.uploadFiles).mockImplementationOnce((() => {
      heldWhenUploadStarted = isComposerSendHeld(slot)
      return new Promise<UploadResult>(r => { uploads.push(r) })
    }) as unknown as typeof api.uploadFiles)
    drag(surface)
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    expect(heldWhenUploadStarted).toBe(true)
    expect(screen.queryByTestId('snip-surface')).toBeNull()
    expect(isComposerSendHeld(slot)).toBe(true)
    expect(sendButton()).toBeDisabled()

    await act(async () => { uploads[0]({ paths: ['/up/snip-1.png'] }) })
    await waitFor(() => expect(isComposerSendHeld(slot)).toBe(false))
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendButton()) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    expect(sentPayloads()).toContain('/up/snip-1.png')
    expect(sentPayloads()).toContain('while snipping')
  })

  it('releases the hold once when the crop is cancelled with Escape', async () => {
    const slot = 'snip-cancel'
    await startSnip(slot)
    await act(async () => { snipSeam.captures[0](fakeFrame) })
    await screen.findByTestId('snip-surface')
    expect(isComposerSendHeld(slot)).toBe(true)

    await act(async () => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(screen.queryByTestId('snip-surface')).toBeNull()
    expect(isComposerSendHeld(slot)).toBe(false)
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
    // A second release for the same snip must not under-count another
    // producer's hold on the slot.
    holdComposerSend(slot)
    await act(async () => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(isComposerSendHeld(slot)).toBe(true)
    releaseComposerSend(slot)
  })

  it('keeps each preview snip hold until its own capture settles', async () => {
    const slot = 'snip-overlap'
    renderHost(<ChatPage key={slot} />, slot)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())

    await act(async () => {
      window.dispatchEvent(new Event('kirocrew-web-preview-snip'))
      window.dispatchEvent(new Event('kirocrew-web-preview-snip'))
    })
    await waitFor(() => expect(snipSeam.captures).toHaveLength(2))
    expect(isComposerSendHeld(slot)).toBe(true)

    await act(async () => { snipSeam.captures[1](null) })
    expect(isComposerSendHeld(slot)).toBe(true)

    await act(async () => { snipSeam.captures[0](null) })
    expect(isComposerSendHeld(slot)).toBe(false)
  })

  it('releases the hold when the share prompt is denied', async () => {
    const slot = 'snip-denied'
    await startSnip(slot)
    expect(isComposerSendHeld(slot)).toBe(true)
    await act(async () => { snipSeam.captures[0](null) })
    expect(screen.queryByTestId('snip-surface')).toBeNull()
    expect(isComposerSendHeld(slot)).toBe(false)
    await waitFor(() => expect(sendButton()).not.toBeDisabled())
  })

  it('releases the hold when the page unmounts under an open crop overlay', async () => {
    const slot = 'snip-unmount'
    const uploads = manualUploads()
    const view = renderHost(<ChatPage key={slot} />, slot)
    await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByTitle('Add files & options')) })
    await act(async () => { fireEvent.click(screen.getByText('Screenshot')) })
    await waitFor(() => expect(snipSeam.captures).toHaveLength(1))
    await act(async () => { snipSeam.captures[0](fakeFrame) })
    await screen.findByTestId('snip-surface')
    expect(isComposerSendHeld(slot)).toBe(true)
    view.unmount()
    expect(isComposerSendHeld(slot)).toBe(false)
    expect(uploads).toHaveLength(0)
  })
})

describe('ChatPane composer: upload hold while the browser is offline', () => {
  it('runs the upload (and so settles the hold) instead of pausing it', async () => {
    // React Query's default networkMode pauses a mutation issued offline before
    // mutationFn: onSettled, the hold's only release, would then never fire and
    // the slot's Send would stay dead across remounts. The mutation must run.
    const onLine = Object.getOwnPropertyDescriptor(navigator, 'onLine')
    Object.defineProperty(navigator, 'onLine', { value: false, configurable: true })
    onlineManager.setOnline(false)
    try {
      const uploads = manualUploads()
      const { container } = renderHost(<ChatPane slotKey="offline-pane" />, 'offline-pane')
      await waitFor(() => expect(container.querySelector('input[type="file"]')).toBeTruthy())
      await act(async () => { pickFile(container, 'offline.txt') })
      await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
      expect(uploads).toHaveLength(1)

      await act(async () => { fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'still here' } }) })
      expect(sendButton()).toBeDisabled()
      await act(async () => { uploads[0]({ paths: ['/up/offline.txt'] }) })
      await waitFor(() => expect(sendButton()).not.toBeDisabled())
    } finally {
      onlineManager.setOnline(true)
      if (onLine) Object.defineProperty(navigator, 'onLine', onLine)
      else delete (navigator as unknown as Record<string, unknown>).onLine
    }
  })
})
