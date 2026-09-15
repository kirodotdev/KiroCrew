/**
 * ChatPane's paste-block sidecar (#11337).
 *
 * `ChatInput` collapses a large paste into a `[ Paste #N · M lines ]` token
 * only when its host passes `onPasteBlocksChange`; the pane rendered it
 * without that prop, so a big paste stayed raw text in split view and member
 * DMs while the main chat showed a chip. These scenes drive the REAL pane and
 * assert against the wire: the token in the composer, the EXPANDED text in
 * the API call, the blocks on the bubble, and the composer clear — plus the
 * lifetime cases that make a token safe: a rebind parks the blocks with the
 * text, and a refused send hands them back with it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { sseChatMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { __resetPaneDraftsForTests, readPaneDraft } from '../utils/chatPaneDrafts'
import { readStoredPaste } from '../utils/pasteTokens'
import { awaitComposer, composerValue, pasteIntoComposer, pressInComposer, setComposerValue, typeIntoComposer } from './helpers'

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
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
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

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const PASTED = 'line1\nline2\nline3\nline4\nline5' // >= PASTE_THRESHOLD_LINES
const TOKEN = /\[ Paste #1 · 5 lines \]/

function makeStore(slotKeys: string[], busy = false) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: slotKeys.map(key => ({ key, messages: 0, running: false, subagents_running: busy, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string, extraSlots: string[] = [], busy = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore([slotKey, ...extraSlots], busy)
  const tree = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={key} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const utils = render(tree(slotKey))
  return { ...utils, store, rebind: (key: string) => utils.rerender(tree(key)) }
}

/**
 * The pane mounts the rich (Lexical) composer on a fine pointer, lazy-loaded
 * behind a fallback, so the driver waits for the real editable root and reads
 * its value through the composer handle — there is no `.value` to read and no
 * `fireEvent.change` to fire. `pressEnter` sends the way a user does.
 */
const composer = () => awaitComposer()
const value = () => composerValue()
const pressEnter = () => pressInComposer('Enter', { code: 'Enter' })

/** Paste through the real handler: the composer's paste command collapses a
 *  big plain-text paste into a pill. */
const pasteInto = (text: string) => pasteIntoComposer(text)

beforeEach(() => {
  vi.clearAllMocks()
  __resetPaneDraftsForTests()
  localStorage.clear()
  sessionStorage.clear()
})

describe('ChatPane paste sidecar', () => {
  it('collapses a large paste to a token, sends it expanded and clears the composer', async () => {
    const { store } = renderPane('pane-paste')
    await composer()
    await setComposerValue('please read ')
    await pasteInto(PASTED)
    // The pill: the composer holds the token, not the pasted lines.
    await waitFor(() => expect(value()).toMatch(TOKEN))
    expect(value()).not.toContain('line3')

    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText, slot, , , meta] = vi.mocked(api.sendChat).mock.calls[0]
    expect(slot).toBe('pane-paste')
    // The model gets the CONTENT, never the token string.
    expect(wireText).toContain(PASTED)
    expect(wireText).not.toMatch(TOKEN)
    // The bubble keeps the token and carries the block so it renders a chip;
    // the side table lets history load re-collapse the server's expanded echo.
    expect(meta.pastes).toEqual([expect.objectContaining({ seq: 1, lines: 5, content: PASTED })])
    const bubble = store.getState().chat.slotMessages['pane-paste']?.find(m => m.role === 'user')
    expect(bubble?.content).toMatch(TOKEN)
    expect(readStoredPaste(wireText as string)?.pastes[0]?.content).toBe(PASTED)
    // Composer and blocks are gone: a second paste starts again at #1.
    await waitFor(() => expect(value()).toBe(''))
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(/\[ Paste #1 · 5 lines \]/))
  })

  it('parks the blocks with the text on a rebind and restores both, so the token still expands', async () => {
    const { rebind } = renderPane('slot-a', ['slot-b'])
    await composer()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))

    // Rebind the same pane instance to another slot: A's composer is parked.
    rebind('slot-b')
    await composer()
    await waitFor(() => expect(value()).toBe(''))
    const parked = readPaneDraft('slot-a')
    expect(parked.text).toMatch(TOKEN)
    expect(parked.pastes).toEqual([expect.objectContaining({ seq: 1, content: PASTED })])

    // Back to A: the token is back AND still backed by its block.
    rebind('slot-a')
    await composer()
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText] = vi.mocked(api.sendChat).mock.calls[0]
    expect(wireText).toContain(PASTED)
    expect(wireText).not.toMatch(TOKEN)
  })

  it('two refused paste sends landing in one batch both come back, each with its own block', async () => {
    // Two sends in flight, both refused, both receipts resolved inside one act:
    // React batches the two recoveries into one commit. Each carries against
    // the blocks the previous one installed (the ref is advanced per
    // recovery), so both tokens keep their content and the retry sends both.
    const settle: Array<(v: unknown) => void> = []
    vi.mocked(api.sendChat).mockImplementation(() => new Promise(resolve => { settle.push(resolve) }) as never)
    const SECOND = 'aa\nbb\ncc\ndd'
    renderPane('pane-batch')
    await composer()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(value()).toBe(''))
    await pasteInto(SECOND)
    await waitFor(() => expect(value()).toMatch(/\[ Paste #1 · 4 lines \]/))
    pressEnter()
    await waitFor(() => expect(settle).toHaveLength(2))
    const refused = { ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) }
    await act(async () => { settle[0](refused); settle[1](refused) })
    // Both tokens are back, re-numbered apart (two blocks cannot share #1).
    await waitFor(() => expect(value()).toMatch(/\[ Paste #1 · \d lines \][\s\S]*\[ Paste #2 · \d lines \]/))
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as never)
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(3))
    const [retryText] = vi.mocked(api.sendChat).mock.calls[2]
    expect(retryText).toContain(PASTED)
    expect(retryText).toContain(SECOND)
    expect(retryText).not.toMatch(/\[ Paste #\d/)
  })

  it('two refused sends in one batch: the second reserves against the text the first already merged, so an inert literal in the first payload is never claimed (round-14 finding)', async () => {
    // Payload 1: block #1 plus a hand-typed literal `[ Paste #7 · 1 lines ]` (no
    // block behind it). Payload 2: typed literal `[ Paste #6 · 1 lines ]` then a
    // paste, which lands at #7 (the paste-time allocator skips the literal).
    // Both refusals resolve in ONE React batch. If the second recovery reserved
    // against the render-time text snapshot it would not see payload 1's
    // literal #7 and would hand block #7 back under 7 — the literal would then
    // name a real block and the retry would expand the user's own text.
    const settle: Array<(v: unknown) => void> = []
    vi.mocked(api.sendChat).mockImplementation(() => new Promise(resolve => { settle.push(resolve) }) as never)
    const SECOND = 'aa\nbb\ncc\ndd'
    const LIT7 = '[ Paste #7 · 1 lines ]'
    const LIT6 = '[ Paste #6 · 1 lines ]'
    renderPane('pane-batch-literal')
    await composer()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    await typeIntoComposer(` ${LIT7}`)
    pressEnter()
    await waitFor(() => expect(value()).toBe(''))
    await setComposerValue(`${LIT6} `)
    await pasteInto(SECOND)
    await waitFor(() => expect(value()).toMatch(/\[ Paste #7 · 4 lines \]/))
    pressEnter()
    await waitFor(() => expect(settle).toHaveLength(2))
    const refused = { ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) }
    await act(async () => { settle[0](refused); settle[1](refused) })
    // Both payloads are back; the second paste's block was moved off 7 because
    // the FIRST payload's literal already carries that number.
    await waitFor(() => expect(value()).toContain(LIT7))
    const restored = value()
    expect(restored).toContain(LIT6)
    expect(restored.match(/\[ Paste #7 · /g)).toHaveLength(1) // only the literal
    expect(restored).toMatch(/\[ Paste #8 · 4 lines \]/)
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as never)
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(3))
    const [retryText] = vi.mocked(api.sendChat).mock.calls[2]
    // Both pastes expand, both literals stay literal.
    expect(retryText).toContain(PASTED)
    expect(retryText).toContain(SECOND)
    expect(retryText).toContain(LIT7)
    expect(retryText).toContain(LIT6)
    expect(retryText).not.toMatch(/\[ Paste #(1|8) · /)
  })

  it('cancelling a queued send restores the paste as its lines, never as a dead token', async () => {
    // A busy pane's send is parked on the queue; the card's cancel hands the
    // composer state back through the send stash, which carries no blocks (the
    // same conservative shape ChatPage restores through) — so the paste comes
    // back EXPANDED: lossless content, and no token left pointing at nothing.
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q-paste' }) } as never)
    const { store } = renderPane('pane-queued', [], true)
    await composer()
    await setComposerValue('later: ')
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    const [wireText] = vi.mocked(api.sendChat).mock.calls[0]
    await waitFor(() => expect(value()).toBe(''))
    // The server's queue card for that send, carrying the wire text.
    act(() => { store.dispatch(sseChatMessage({ slot: 'pane-queued', role: 'queued', content: wireText as string, meta: { queueId: 'q-paste' } })) })
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel queued message' }))
    await waitFor(() => expect(value()).toContain(PASTED))
    expect(value()).not.toMatch(/\[ Paste #\d/)
    // The retry sends exactly that content.
    vi.mocked(api.sendChat).mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) } as never)
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    const [retryText] = vi.mocked(api.sendChat).mock.calls[1]
    expect(retryText).toContain(PASTED)
    expect(retryText).not.toMatch(TOKEN)
  })

  it('hands the blocks back with the text when the server refuses the send', async () => {
    vi.mocked(api.sendChat).mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ ok: false, error: 'refused' }) } as never)
    renderPane('pane-refused')
    await composer()
    await pasteInto(PASTED)
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(1))
    // The refused payload is restored — token text AND its block — so the
    // retry sends the content, not a dead token.
    await waitFor(() => expect(value()).toMatch(TOKEN))
    pressEnter()
    await waitFor(() => expect(api.sendChat).toHaveBeenCalledTimes(2))
    const [retryText] = vi.mocked(api.sendChat).mock.calls[1]
    expect(retryText).toContain(PASTED)
    expect(retryText).not.toMatch(TOKEN)
  })
})
