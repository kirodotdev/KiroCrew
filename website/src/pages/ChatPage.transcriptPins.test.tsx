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
import type { DisplayItem } from './chat/types'
import { useTranscriptPins } from './chat/page/transcriptPins'

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
    scrollerRef: { current: null }, search: { isOpen: false } as never, navToDisplayIndex: vi.fn(), pinnedJumpChrome: () => 0,
    scrollToPinnedPrompt: vi.fn(), setHighlightTs: vi.fn(), splitFeatureEnabled: false, regenerating: false, setEditLast: vi.fn(),
    editSeqRef: { current: 0 }, renderedDisplayItems: rows(messages), renderedTranscript: { messages, displayItems: rows(messages) },
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
  it('scrolls to the row, highlights it, and clears the highlight after three seconds', () => {
    vi.useFakeTimers()
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('t2', 'm2') })
    expect(p.navToDisplayIndex).toHaveBeenCalledWith(1, { behavior: 'smooth', align: 'center' })
    expect(p.setHighlightTs).toHaveBeenLastCalledWith('t2')
    act(() => { vi.advanceTimersByTime(3000) })
    expect(p.setHighlightTs).toHaveBeenLastCalledWith(null)
  })

  it('resolves by mid when the pin carries one, and by ts only for a legacy pin', () => {
    const twins = [msg('user', 'same', 'a'), msg('user', 'same', 'b')]
    const p = props({ messages: twins })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('same', 'b') })
    expect(p.navToDisplayIndex).toHaveBeenLastCalledWith(1, expect.anything())
    act(() => { result.current.handleJumpToPin('same') })
    expect(p.navToDisplayIndex).toHaveBeenLastCalledWith(0, expect.anything())
  })

  it('jumps a sent quote card to its row, and ignores a quote with no ts', () => {
    const p = props()
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToQuote({ role: 'assistant', content: 'x' } as never) })
    expect(p.navToDisplayIndex).not.toHaveBeenCalled()
    act(() => { result.current.handleJumpToQuote({ role: 'assistant', content: 'x', ts: 't2', mid: 'm2' } as never) })
    expect(p.navToDisplayIndex).toHaveBeenCalledWith(1, expect.anything())
  })
})

describe('a target outside the loaded rows', () => {
  it('reports the pin as unavailable at once when there is no older history', () => {
    const p = props({ slotHasMore: false })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleJumpToPin('gone', 'gone') })
    expect(result.current.pinStatus).toBe(i18nT('pages.chat.pins.message_unavailable'))
    expect(p.navToDisplayIndex).not.toHaveBeenCalled()
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

describe('the Cmd/Ctrl+Up edit request', () => {
  it('opens the newest user message in the editor, scrolling it into view first', () => {
    const p = props({ messages: [msg('user', 'u1'), msg('assistant', 'a1'), msg('user', 'u2'), msg('assistant', 'a2')] })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleEditLastRequest() })
    expect(p.navToDisplayIndex).toHaveBeenCalledWith(2, { behavior: 'auto', align: 'center' })
    expect(p.setEditLast).toHaveBeenCalledWith({ seq: 1, ts: 'u2', index: 2 })
  })

  it('does nothing while a turn runs', () => {
    const p = props({ slotRunning: true })
    const { result } = renderHook(() => useTranscriptPins(p), { wrapper: withQueryClient() })
    act(() => { result.current.handleEditLastRequest() })
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
