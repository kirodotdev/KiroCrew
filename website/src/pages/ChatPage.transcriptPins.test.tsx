/**
 * Transcript navigation (pages/chat/page/transcriptPins.ts), driven directly:
 * jumping to a pinned, quoted or last-edited message — on the loaded rows, or by
 * paging older history until the target lands — the notice each entry point owes
 * the reader when it cannot, and pin / unpin.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const pins = vi.hoisted(() => ({
  state: {
    pins: [] as Array<{ id: string }>,
    error: null as null | 'pin' | 'pin_limit' | 'unpin',
    isPinned: (_mid: string) => false,
  },
  pinMessage: vi.fn(async () => undefined),
  unpinMessage: vi.fn(async () => undefined),
  unpinById: vi.fn(async () => undefined),
  clearError: vi.fn(),
}))
vi.mock('../hooks/useChatPins', () => ({
  useChatPins: () => ({
    pins: pins.state.pins, loading: false, error: pins.state.error, clearError: pins.clearError, isPinned: pins.state.isPinned,
    pinMessage: pins.pinMessage, unpinMessage: pins.unpinMessage, unpinById: pins.unpinById, refresh: vi.fn(),
  }),
}))
vi.mock('../hooks/usePanelTabs', async (orig) => ({ ...(await orig<typeof import('../hooks/usePanelTabs')>()), openPanelView: vi.fn() }))
// Spied so a test can wait for the rejection handler to have run before asserting it did nothing.
vi.mock('../store/chatSlice', async (orig) => {
  const real = await orig<typeof import('../store/chatSlice')>()
  return { ...real, isSupersededPagingRejection: vi.fn(real.isSupersededPagingRejection) }
})

import { openPanelView } from '../hooks/usePanelTabs'
import { i18nT } from '../i18n/t'
import { isSupersededPagingRejection, openActivityPanel } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { formatToken } from '../utils/pasteTokens'
import { prependQuote } from '../chat-core/composer/messageQuote'
import type { DisplayItem } from './chat/types'
import { useTranscriptPins } from './chat/page/transcriptPins'
import { useChatPageTranscriptEarlyController } from './chat/useChatPageTranscriptController'

type Props = Parameters<typeof useTranscriptPins>[0]

/** One query client per rendered hook: the navigation hooks read through React Query. */
function withQueryClient() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const msg = (role: string, ts: string, mid?: string) =>
  ({ role, content: `${role} ${ts}`, ts, cls: '', ...(mid ? { meta: { mid } } : {}) }) as unknown as ChatMessage
const rows = (messages: ChatMessage[]): DisplayItem[] => messages.map((m, idx) => ({ kind: 'single', msg: m, idx }))

/** A dispatch whose thunks answer with the page the test hands it. */
function pagingDispatch(page: () => Promise<unknown>) {
  return vi.fn((action: unknown) => (typeof action === 'function' ? { unwrap: page } : action))
}

function props(over: Partial<Props> = {}): Props {
  const messages = over.messages ?? [msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')]
  return {
    embedded: false, dispatch: pagingDispatch(async () => null) as never, isMobile: false, activeSlot: 'slot-a', messages,
    messagesRef: { current: messages }, slotRunning: false, splitMode: false, crewWindow: null, slotHasMore: false, slotOldestIndex: 0,
    cursorIsForActiveSlot: true, loadingOlder: false, activeViewIsBoundedPage: false, isWelcomeState: false, activeSlotRemoteBound: false,
    scrollerRef: { current: null }, search: { isOpen: false } as never, navToDisplayIndex: vi.fn(), navToMessage: vi.fn(() => true), pinnedJumpChrome: () => 0,
    scrollToPinnedPrompt: vi.fn(), markStep: vi.fn(), markJump: vi.fn(), splitFeatureEnabled: false, regenerating: false, setEditLast: vi.fn(),
    editSeqRef: { current: 0 }, renderedDisplayItems: rows(messages), renderedTranscript: { messages, displayItems: rows(messages) },
    rowsTrailList: false, insertIntoComposer: vi.fn(),
    ...over,
  }
}

beforeEach(() => {
  pins.state.pins = []
  pins.state.error = null
  pins.state.isPinned = () => false
  vi.clearAllMocks()
})
afterEach(() => { vi.useRealTimers() })

describe('jumping to a loaded message', () => {
  it('scrolls to the row and marks it with the flash', () => {
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('t2', 'm2') })
    expect(p.navToMessage).toHaveBeenCalledWith(p.messages[1], { behavior: 'smooth', align: 'center' })
    expect(p.markJump).toHaveBeenLastCalledWith('t2', false, 'm2')
  })

  it('gives a link jump the landing highlight', () => {
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPinnedMessage('t2', 'm2', { origin: 'link' }) })
    expect(p.markJump).toHaveBeenLastCalledWith('t2', true, 'm2')
  })

  it('resolves by mid when the pin carries one, and by ts only for a legacy pin', () => {
    const twins = [msg('user', 'same', 'a'), msg('user', 'same', 'b')]
    const p = props({ messages: twins })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('same', 'b') })
    expect(p.navToMessage).toHaveBeenLastCalledWith(twins[1], expect.anything())
    // The mark carries the chosen twin's mid, so only that row is highlighted.
    expect(p.markJump).toHaveBeenLastCalledWith('same', false, 'b')
    act(() => { result.current.handleJumpToPin('same') })
    expect(p.navToMessage).toHaveBeenLastCalledWith(twins[0], expect.anything())
  })

  it('jumps a sent quote card to its row, and ignores a quote with no ts', () => {
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToQuote({ role: 'assistant', content: 'x' } as never) })
    expect(p.navToMessage).not.toHaveBeenCalled()
    act(() => { result.current.handleJumpToQuote({ role: 'assistant', content: 'x', ts: 't2', mid: 'm2' } as never) })
    expect(p.navToMessage).toHaveBeenCalledWith(p.messages[1], expect.anything())
  })
})

describe('a target outside the loaded rows', () => {
  it('reports the pin as unavailable at once when there is no older history', () => {
    const p = props({ slotHasMore: false })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('gone', 'gone') })
    expect(result.current.pinStatus).toBe(i18nT('pages.chat.pins.message_unavailable'))
    expect(p.navToMessage).not.toHaveBeenCalled()
  })

  it('treats a loaded message that no row on screen draws as not there: no mark, the same notice', () => {
    const p = props({ navToMessage: vi.fn(() => false) })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('t2', 'm2') })
    expect(p.navToMessage).toHaveBeenCalledWith(p.messages[1], expect.anything())
    expect(p.markJump).not.toHaveBeenCalled()
    expect(result.current.pinStatus).toBe(i18nT('pages.chat.pins.message_unavailable'))
  })

  // The rows on screen draw the deferred snapshot, a commit behind the list
  // while a landed page waits to render: a loaded message with no row yet is
  // not gone, and the jump waits for the rows.
  it('waits while the rows on screen trail the list, then jumps once they draw the message', () => {
    const navToMessage = vi.fn(() => false)
    const markJump = vi.fn()
    const base = props({ navToMessage, markJump })
    const { result, rerender } = renderHook((over: Partial<Props>) => useTranscriptPins({ ...base, ...over }), {
      initialProps: { rowsTrailList: true } as Partial<Props>, wrapper: withQueryClient(),
    })
    act(() => { result.current.handleJumpToPin('t2', 'm2') })
    expect(navToMessage).toHaveBeenCalledWith(base.messages[1], expect.anything())
    expect(markJump).not.toHaveBeenCalled()
    expect(result.current.pinStatus).toBeNull()
    // The rows catch up and draw it.
    navToMessage.mockReturnValue(true)
    rerender({ rowsTrailList: false })
    expect(markJump).toHaveBeenCalledWith('t2', false, 'm2')
    expect(result.current.pinStatus).toBeNull()
  })

  it('a jump that pages the oldest page in waits for that page to draw, instead of reporting the message gone', async () => {
    const loaded = [msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')]
    const navToMessage = vi.fn(() => false)
    const markJump = vi.fn()
    const dispatch = pagingDispatch(async () => ({}))
    const base = props({ messages: loaded, navToMessage, markJump, dispatch: dispatch as never, slotHasMore: true, slotOldestIndex: 40 })
    const { result, rerender } = renderHook((over: Partial<Props>) => useTranscriptPins({ ...base, ...over }), {
      initialProps: {} as Partial<Props>, wrapper: withQueryClient(),
    })
    act(() => { result.current.handleJumpToPin('o1', 'om1') })
    await waitFor(() => expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(true))
    // The last page lands with the message: the list has it, the history is
    // exhausted, and the rows on screen still draw the list from before it.
    const landed = [msg('user', 'o1', 'om1'), ...loaded]
    const afterLanding = { messages: landed, messagesRef: { current: landed }, slotHasMore: false, slotOldestIndex: 0 }
    rerender({ ...afterLanding, rowsTrailList: true })
    expect(result.current.pinStatus).toBeNull()
    expect(markJump).not.toHaveBeenCalled()
    // The rows catch up and draw it.
    navToMessage.mockReturnValue(true)
    rerender({ ...afterLanding, rowsTrailList: false })
    expect(markJump).toHaveBeenCalledWith('o1', false, 'om1')
    expect(result.current.pinStatus).toBeNull()
  })

  it('pages older history and reports not-found in the entry point own wording when history runs out', async () => {
    const dispatch = pagingDispatch(async () => null)
    const p = props({ slotHasMore: true, slotOldestIndex: 40, dispatch: dispatch as never })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToQuote({ role: 'user', content: 'x', ts: 'old', mid: 'old' } as never) })
    await waitFor(() => expect(result.current.pinStatus).toBe(i18nT('pages.chat.quoteCard.message_unavailable')))
    expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(true)
    expect(result.current.pinError).toBeNull()
  })

  it('reports a failed page as a load error for the earlier-messages entry point, never as not-found', async () => {
    const p = props({ slotHasMore: true, slotOldestIndex: 40, dispatch: pagingDispatch(async () => { throw new Error('502') }) as never })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.setPendingPinnedJump({ slotKey: 'slot-a', messageTs: 'old', mid: 'old', origin: 'earlier' }) })
    await waitFor(() => expect(result.current.pinError).toBe(i18nT('components.chatPane.earlier_messages_load_failed')))
    expect(result.current.pinStatus).toBeNull()
  })

  it('treats a superseded page (the user switched chat) as no answer at all', async () => {
    const p = props({
      slotHasMore: true, slotOldestIndex: 40,
      dispatch: pagingDispatch(async () => { throw Object.assign(new Error('aborted'), { name: 'AbortError' }) }) as never,
    })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('old', 'old') })
    await waitFor(() => expect(isSupersededPagingRejection).toHaveBeenCalled())
    expect(result.current.pinError).toBeNull()
    expect(result.current.pinStatus).toBeNull()
  })

  it('drops a pending jump that belongs to another slot', () => {
    const dispatch = pagingDispatch(async () => null)
    const p = props({ slotHasMore: true, slotOldestIndex: 40, dispatch: dispatch as never })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.setPendingPinnedJump({ slotKey: 'slot-b', messageTs: 'old', origin: 'pin' }) })
    expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(false)
    expect(result.current.pinStatus).toBeNull()
  })

  it('waits while the cursor still describes the chat that was left, and while a page is in flight', () => {
    const dispatch = pagingDispatch(async () => null)
    const { result, rerender } = renderHook((over: Partial<Props>) => useTranscriptPins(props({ slotHasMore: true, slotOldestIndex: 40, dispatch: dispatch as never, ...over })), {
      initialProps: { cursorIsForActiveSlot: false } as Partial<Props>, wrapper: withQueryClient(),
    })
    act(() => { result.current.setPendingPinnedJump({ slotKey: 'slot-a', messageTs: 'old', origin: 'link' }) })
    rerender({ cursorIsForActiveSlot: true, loadingOlder: true })
    expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(false)
    expect(result.current.pinStatus).toBeNull()
  })
})

describe('putting a pinned message in the composer', () => {
  it('puts the message as sent in the composer once and jumps to the row; a plain Jump does not fill', () => {
    const block = { id: 'p1', seq: 1, lines: 3, content: 'a\nb\nc' }
    const prompt = { role: 'user', content: `rerun ${formatToken(block)}`, ts: 't1', cls: '', meta: { mid: 'm1', pastes: [block] } } as unknown as ChatMessage
    const p = props({ messages: [prompt, msg('assistant', 't2', 'm2')] })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handlePutPinInComposer('t1', 'm1') })
    expect(p.insertIntoComposer).toHaveBeenCalledWith('rerun a\nb\nc')
    expect(p.navToMessage).toHaveBeenCalledWith(prompt, { behavior: 'smooth', align: 'center' })
    act(() => { result.current.handleJumpToPin('t1', 'm1') })
    expect(p.insertIntoComposer).toHaveBeenCalledTimes(1)
  })

  it('keeps the reply a pinned message quoted, as edit-and-resend does', () => {
    const quote = { role: 'assistant' as const, text: 'the plan', ts: 't0', mid: 'm0' }
    const prompt = { role: 'user', content: prependQuote('do step 2', quote), ts: 't1', cls: '', meta: { mid: 'm1', quote } } as unknown as ChatMessage
    const p = props({ messages: [prompt] })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handlePutPinInComposer('t1', 'm1') })
    expect(p.insertIntoComposer).toHaveBeenCalledWith(prependQuote('do step 2', quote))
  })

  it('never fills from a reply that a legacy pin resolves to by its ts', () => {
    const p = props({ messages: [msg('assistant', 'same', 'a'), msg('user', 'same', 'b')] })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handlePutPinInComposer('same') })
    expect(p.navToMessage).toHaveBeenCalledWith(p.messages[0], expect.anything())
    expect(p.insertIntoComposer).not.toHaveBeenCalled()
  })

  it('pages a distant pin in and fills the composer exactly once, through the bounded-page re-resolve', async () => {
    const dispatch = pagingDispatch(async () => ({}))
    const insertIntoComposer = vi.fn()
    const base = { slotHasMore: true, slotOldestIndex: 40, dispatch: dispatch as never, insertIntoComposer }
    const { result, rerender } = renderHook((over: Partial<Props>) => useTranscriptPins(props({ ...base, ...over })), {
      initialProps: {} as Partial<Props>, wrapper: withQueryClient(),
    })
    act(() => { result.current.handlePutPinInComposer('old', 'old') })
    await waitFor(() => expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(true))
    expect(insertIntoComposer).not.toHaveBeenCalled()
    const paged = [msg('user', 'old', 'old'), msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')]
    rerender({ messages: paged, activeViewIsBoundedPage: true })
    rerender({ messages: [...paged], activeViewIsBoundedPage: false })
    expect(insertIntoComposer).toHaveBeenCalledTimes(1)
    expect(insertIntoComposer).toHaveBeenCalledWith('user old')
  })

  it('drops a pending fill when a newer jump lands on a loaded row before the page arrives', async () => {
    const dispatch = pagingDispatch(async () => ({}))
    const insertIntoComposer = vi.fn()
    const base = { slotHasMore: true, slotOldestIndex: 40, dispatch: dispatch as never, insertIntoComposer }
    const { result, rerender } = renderHook((over: Partial<Props>) => useTranscriptPins(props({ ...base, ...over })), {
      initialProps: {} as Partial<Props>, wrapper: withQueryClient(),
    })
    act(() => { result.current.handlePutPinInComposer('old', 'old') })
    await waitFor(() => expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(true))
    act(() => { result.current.handleJumpToPin('t1', 'm1') })
    rerender({ messages: [msg('user', 'old', 'old'), msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')] })
    expect(insertIntoComposer).not.toHaveBeenCalled()
  })

  it.each([
    ['Split View', { splitMode: true, splitFeatureEnabled: true }],
    ['a crew window', { crewWindow: { instanceId: 'i1', key: 'k1' } }],
  ] as const)('drops a pending fill whose page lands while %s hides the composer', async (_name, hidden) => {
    const dispatch = pagingDispatch(async () => ({}))
    const insertIntoComposer = vi.fn()
    const navToMessage = vi.fn(() => true)
    const base = { slotHasMore: true, slotOldestIndex: 40, dispatch: dispatch as never, insertIntoComposer, navToMessage }
    const { result, rerender } = renderHook((over: Partial<Props>) => useTranscriptPins(props({ ...base, ...over })), {
      initialProps: {} as Partial<Props>, wrapper: withQueryClient(),
    })
    act(() => { result.current.handlePutPinInComposer('old', 'old') })
    await waitFor(() => expect(dispatch.mock.calls.some(([a]) => typeof a === 'function')).toBe(true))
    const paged = [msg('user', 'old', 'old'), msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')]
    rerender({ ...hidden, messages: paged })
    expect(navToMessage).toHaveBeenCalledWith(paged[0], { behavior: 'smooth', align: 'center' })
    expect(insertIntoComposer).not.toHaveBeenCalled()
  })
})

describe('the Cmd/Ctrl+Up edit request', () => {
  it('opens the newest user message in the editor, scrolling it into view first', () => {
    const p = props({ messages: [msg('user', 'u1'), msg('assistant', 'a1'), msg('user', 'u2'), msg('assistant', 'a2')] })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleEditLastRequest() })
    expect(p.navToMessage).toHaveBeenCalledWith(p.messages[2], { behavior: 'auto', align: 'center' })
    expect(p.setEditLast).toHaveBeenCalledWith({ seq: 1, ts: 'u2', index: 2 })
  })

  it('does nothing while a turn runs', () => {
    const p = props({ slotRunning: true })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleEditLastRequest() })
    expect(p.setEditLast).not.toHaveBeenCalled()
  })

  it('opens no editor while the newest user message has no row on screen', () => {
    const p = props({
      messages: [msg('user', 'u1'), msg('assistant', 'a1'), msg('user', 'u2'), msg('assistant', 'a2')],
      navToMessage: vi.fn(() => false),
    })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleEditLastRequest() })
    expect(p.navToMessage).toHaveBeenCalledWith(p.messages[2], { behavior: 'auto', align: 'center' })
    expect(p.setEditLast).not.toHaveBeenCalled()
  })
})

describe('pinning', () => {
  it('unpins a pinned message, and opens the Pins tab quietly on a session first pin', () => {
    pins.state.isPinned = (mid) => mid === 'm1'
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleTogglePinForMessage('m1', 't1', 'user', 'hi') })
    expect(pins.unpinMessage).toHaveBeenCalledWith('m1')
    act(() => { result.current.handleTogglePinForMessage('m2', 't2', 'assistant', 'there') })
    expect(pins.pinMessage).toHaveBeenCalledWith({ mid: 'm2', message_ts: 't2', role: 'assistant', preview: 'there' })
    expect(openPanelView).toHaveBeenCalledWith('slot-a', 'pins')
    expect(p.dispatch).toHaveBeenCalledWith(openActivityPanel())
  })

  it('does not open the panel over an open find pane or on a phone, and not after the first pin', () => {
    pins.state.pins = [{ id: 'p1' }]
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleTogglePinForMessage('m2', 't2', 'assistant', 'there') })
    expect(openPanelView).not.toHaveBeenCalled()
    pins.state.pins = []
    const mobile = props({ isMobile: true })
    const r2 = renderHook(() => useTranscriptPins(mobile), { wrapper: withQueryClient() }).result
    act(() => { r2.current.handleTogglePinForMessage('m2', 't2', 'assistant', 'there') })
    expect(openPanelView).toHaveBeenCalledWith('slot-a', 'pins')
    expect(mobile.dispatch).not.toHaveBeenCalledWith(openActivityPanel())
  })

  it('unpins from the Pins list by id', () => {
    const { result } = renderHook(() => useTranscriptPins(props()), { wrapper: withQueryClient() })
    act(() => { result.current.handleUnpinById('p9') })
    expect(pins.unpinById).toHaveBeenCalledWith('p9')
  })

  it('splits refused requests (errors) from answers (status), and lets only the answer time out', () => {
    vi.useFakeTimers()
    pins.state.error = 'pin'
    const failed = renderHook(() => useTranscriptPins(props()), { wrapper: withQueryClient() }).result
    expect(failed.current.pinError).toBe(i18nT('pages.chat.pins.pin_failed'))
    expect(failed.current.pinStatus).toBeNull()

    pins.state.error = 'pin_limit'
    const limited = renderHook(() => useTranscriptPins(props()), { wrapper: withQueryClient() }).result
    expect(limited.current.pinError).toBeNull()
    expect(limited.current.pinStatus).toBe(i18nT('pages.chat.pins.pin_limit_reached'))
    act(() => { vi.advanceTimersByTime(8000) })
    expect(pins.clearError).toHaveBeenCalled()

    act(() => { limited.current.dismissPinStatus() })
    expect(pins.clearError).toHaveBeenCalledTimes(2)
  })
})

describe('turn navigation', () => {
  it('scrubs instantly from the minimap and glides otherwise', () => {
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    result.current.navigateToTurn(1, { instant: true })
    expect(p.navToDisplayIndex).toHaveBeenLastCalledWith(1, { behavior: 'auto', align: 'start', offset: -24 })
    result.current.navigateToTurn(0)
    expect(p.navToDisplayIndex).toHaveBeenLastCalledWith(0, { behavior: 'smooth', align: 'start', offset: -24 })
    expect(result.current.messageToDisplayIdx.get(1)).toBe(1)
  })
})

// The jumps above through the page's own row scroll (the early transcript
// controller), over a scroller whose rows carry their display index. The scroll
// itself is a spy, so each step of the jump's settle poll says which row it aimed
// at. A page of older history lands while that poll is still stepping and
// renumbers every row: the jump must stay on its message's row.
describe('a jump stays on its message while a landing page renumbers the rows', () => {
  let frames: FrameRequestCallback[] = []
  let restore: () => void = () => {}
  beforeEach(() => {
    frames = []
    const raf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => frames.push(cb)) as typeof requestAnimationFrame
    // The poll ends once its quiet window has passed on this clock; frozen, it
    // keeps stepping until the test has landed the page.
    const now = vi.spyOn(performance, 'now').mockReturnValue(0)
    restore = () => { globalThis.requestAnimationFrame = raf; now.mockRestore() }
  })
  afterEach(() => {
    restore()
    document.body.replaceChildren()
  })
  const flush = (n: number) => {
    for (let i = 0; i < n; i++) {
      const batch = frames
      frames = []
      act(() => { batch.forEach((cb) => cb(0)) })
    }
  }

  const older = [msg('user', 'o1', 'om1'), msg('assistant', 'o2', 'om2'), msg('user', 'o3', 'om3')]

  function mount(loaded: ChatMessage[]) {
    const scroller = document.createElement('div')
    document.body.append(scroller)
    const scrollToDisplayIndex = vi.fn(() => true)
    const mountIndex = vi.fn(() => false)
    const page = {
      activeTip: null,
      mountIndexRef: { current: mountIndex },
      estimateRowTopRef: { current: () => null },
      scrollerRef: { current: scroller },
      scrollToDisplayIndex,
      slotRunningRef: { current: false },
      vGetFollowRef: { current: () => false },
      vScrollToBottomRef: { current: () => {} },
    }
    const hook = renderHook(({ messages }: { messages: ChatMessage[] }) => {
      const early = useChatPageTranscriptEarlyController(page as never)
      const pins = useTranscriptPins(props({
        messages, navToDisplayIndex: early.navToDisplayIndex, navToMessage: early.navToMessage,
      }))
      return { early, pins }
    }, { initialProps: { messages: loaded }, wrapper: withQueryClient() })
    /** A commit of the transcript: the hooks get `list`, and its rows are the ones on screen. */
    const draw = (list: ChatMessage[]) => act(() => {
      hook.rerender({ messages: list })
      hook.result.current.early.displayItemsRef.current = rows(list)
      scroller.replaceChildren(...list.map((_, i) => {
        const row = document.createElement('div')
        row.dataset.displayIndex = String(i)
        return row
      }))
    })
    draw(loaded)
    /** The virtualizer dropped row `di`: the rows on screen still list it, its element is gone. */
    const unmountRow = (di: number) => act(() => { scroller.querySelector(`[data-display-index="${di}"]`)?.remove() })
    /** The display row each step of the poll scrolled to, so far. */
    const aims = () => scrollToDisplayIndex.mock.calls.map(([di]) => di as number)
    /** The display row each mount asked for, so far. */
    const mounts = () => mountIndex.mock.calls.map(([di]) => di as number)
    return { pins: () => hook.result.current.pins, draw, unmountRow, aims, mounts, scrollToDisplayIndex, mountIndex }
  }

  const loaded = () => [msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')]

  it.each([
    ['a pin', (p: ReturnType<typeof useTranscriptPins>) => p.handleJumpToPin('t2', 'm2')],
    ['a quote card', (p: ReturnType<typeof useTranscriptPins>) => p.handleJumpToQuote({ role: 'assistant', content: 'x', ts: 't2', mid: 'm2' } as never)],
    ['a link from another surface', (p: ReturnType<typeof useTranscriptPins>) => p.handleJumpToPinnedMessage('t2', 'm2', { origin: 'link' })],
  ] as const)('%s: the poll follows the message to its new row', (_name, jump) => {
    const rowsNow = loaded()
    const view = mount(rowsNow)
    act(() => { jump(view.pins()) })
    flush(1)
    expect(view.aims()).toEqual([1])
    view.draw([...older, ...rowsNow])
    flush(2)
    expect(view.aims()).toEqual([1, 4, 4])
    expect(view.scrollToDisplayIndex).toHaveBeenLastCalledWith(4, { behavior: 'auto', align: 'center' })
  })

  it('finds the message by its key when the landing replaced its object, as the store does on an update', () => {
    const rowsNow = loaded()
    const view = mount(rowsNow)
    act(() => { view.pins().handleJumpToPin('t2', 'm2') })
    flush(1)
    view.draw([...older, ...rowsNow.map((m) => ({ ...m }))])
    flush(1)
    expect(view.aims()).toEqual([1, 4])
  })

  it('the Cmd/Ctrl+Up edit request scrolls to the newest user message after the landing too', () => {
    const rowsNow = [msg('user', 'u1'), msg('assistant', 'a1'), msg('user', 'u2'), msg('assistant', 'a2')]
    const view = mount(rowsNow)
    act(() => { view.pins().handleEditLastRequest() })
    flush(1)
    view.draw([...older, ...rowsNow])
    flush(1)
    expect(view.aims()).toEqual([2, 5])
  })

  it('a turn marker keeps its start alignment on the turn it named', () => {
    const rowsNow = loaded()
    const view = mount(rowsNow)
    act(() => { view.pins().navigateToTurn(1) })
    flush(1)
    expect(view.scrollToDisplayIndex).toHaveBeenLastCalledWith(1, { behavior: 'smooth', align: 'start', offset: -24 })
    view.draw([...older, ...rowsNow])
    flush(1)
    expect(view.scrollToDisplayIndex).toHaveBeenLastCalledWith(4, { behavior: 'auto', align: 'start', offset: -24 })
  })

  it('waits, scrolling nothing, while the message has no row on screen, then aims at its row', () => {
    const rowsNow = loaded()
    const view = mount(rowsNow)
    act(() => { view.pins().handleJumpToPin('t2', 'm2') })
    // The rows on screen stop drawing the message for a while.
    view.draw([rowsNow[0]])
    flush(2)
    expect(view.aims()).toEqual([])
    view.draw([...older, ...rowsNow])
    flush(1)
    expect(view.aims()).toEqual([4])
  })

  // A landing that commits in the jump's frame recomputes the window from the
  // scroll position the jump has not moved yet, and unmounts the far row the
  // jump had just mounted. The poll mounts it again instead of idling out.
  it('mounts the row again when it is gone before the first step, then scrolls to it', () => {
    const rowsNow = loaded()
    const view = mount(rowsNow)
    view.mountIndex.mockClear()
    act(() => { view.pins().handleJumpToPin('t2', 'm2') })
    expect(view.mounts()).toEqual([1])
    // The row's element is not in the scroller: the window moved on without it.
    view.unmountRow(1)
    flush(1)
    expect(view.aims()).toEqual([])
    expect(view.mounts()).toEqual([1, 1])
    // The mount took: the row is back, and the poll scrolls to it.
    view.draw(rowsNow)
    flush(1)
    expect(view.aims()).toEqual([1])
  })
})
