/**
 * The transcript renderers memo in transcriptRendering.tsx lists the page's
 * row callbacks as deps, and every settled TurnBlock and AssistantMessage
 * bails out of a re-render only while that memo's value keeps its identity.
 * Three of those callbacks used to close over `messages`, which changes on
 * every streamed frame (a chunk, a tool call, a tool result), so each frame
 * rebuilt the memo and re-rendered EVERY mounted row. On a long session that
 * pinned the main thread for seconds per turn and grew the heap by hundreds of
 * MB, until the dashboard went blank.
 *
 * These tests hold each callback's identity across a `messages` change and
 * check it still acts on the newest rows, read through the page's one
 * `messagesRef`. The ref here is a single object updated per render, the way
 * activeSlotState.ts keeps the page's.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

const calls = vi.hoisted(() => ({
  regenerateSlot: vi.fn(async () => undefined),
  rewindWithRollback: vi.fn(),
}))
vi.mock('../api/client', async (orig) => {
  const real = await orig<typeof import('../api/client')>()
  return { ...real, api: { ...real.api, regenerateSlot: calls.regenerateSlot } }
})
vi.mock('../lib/rewindCall', () => ({ rewindWithRollback: calls.rewindWithRollback }))
vi.mock('../hooks/useChatPins', () => ({
  useChatPins: () => ({
    pins: [], loading: false, error: null, clearError: vi.fn(), isPinned: () => false,
    pinMessage: vi.fn(), unpinMessage: vi.fn(), unpinById: vi.fn(), refresh: vi.fn(),
  }),
}))

import { store } from '../store'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'
import { useRegenerate } from '../pages/chat/page/turnActions'
import { useTranscriptPins } from '../pages/chat/page/transcriptPins'
import { useMessageActions } from '../pages/chat/page/messageActions'

const msg = (role: string, ts: string, mid?: string): ChatMessage =>
  ({ role, content: `${role} ${ts}`, ts, cls: '', ...(mid ? { meta: { mid } } : {}) }) as unknown as ChatMessage
const rows = (messages: ChatMessage[]): DisplayItem[] => messages.map((m, idx) => ({ kind: 'single', msg: m, idx }))

const first = [msg('user', 't1', 'm1'), msg('assistant', 't2', 'm2')]
/** The next streamed frame: a new array, as the store hands one over per frame. */
const next = [...first, msg('user', 't3', 'm3'), msg('assistant', 't4', 'm4')]

/** One ref per rendered hook, re-pointed on each render like the page's own. */
function latest(messagesRef: { current: ChatMessage[] }, messages: ChatMessage[]) {
  messagesRef.current = messages
  return messagesRef
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <Provider store={store}>
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    </Provider>
  )
}

beforeEach(() => { vi.clearAllMocks() })

describe('row callbacks keep their identity across streamed frames', () => {
  it('useRegenerate: handleRegenerate is stable and truncates against the newest rows', () => {
    const dispatch = vi.fn()
    const base = {
      dispatch: dispatch as never, activeSlot: 'slot-a', slotRunning: false, activeSlotRemoteBound: false,
      isStreaming: false, setRefusedPress: vi.fn(), showRefusedPress: vi.fn(),
    }
    const ref = { current: first }
    const { result, rerender } = renderHook((p: { messages: ChatMessage[] }) => useRegenerate({ ...base, ...p, messagesRef: latest(ref, p.messages) }), {
      initialProps: { messages: first },
    })
    const before = result.current.handleRegenerate
    rerender({ messages: next })
    expect(result.current.handleRegenerate).toBe(before)
    act(() => { result.current.handleRegenerate() })
    // The newest turn is regenerated: truncate after the LAST user row (index 2),
    // which only the second frame holds.
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'chat/truncateAfterIndex', payload: 3 }))
    expect(calls.regenerateSlot).toHaveBeenCalledWith('slot-a')
  })

  it('useRegenerate: steps past a trailing taken-back steer to regenerate its reply', () => {
    const dispatch = vi.fn()
    const messages = [
      msg('user', 't1', 'm1'),
      msg('assistant', 't2', 'm2'),
      { ...msg('user', 't3', 'm3'), content: '', meta: { mid: 'm3', steer: true, steerState: 'retracted' } },
    ]
    const ref = { current: messages }
    const { result } = renderHook(() => useRegenerate({
      dispatch: dispatch as never,
      activeSlot: 'slot-a',
      messages,
      messagesRef: ref,
      slotRunning: false,
      activeSlotRemoteBound: false,
      isStreaming: false,
      setRefusedPress: vi.fn(),
      showRefusedPress: vi.fn(),
    }))

    act(() => { result.current.handleRegenerate() })

    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'chat/truncateAfterIndex', payload: 1 }))
    expect(calls.regenerateSlot).toHaveBeenCalledWith('slot-a')
  })

  it('useRegenerate: truncates before a taken-back steer when a later reply exists', () => {
    const dispatch = vi.fn()
    const messages = [
      msg('user', 't1', 'm1'),
      msg('assistant', 't2', 'm2'),
      { ...msg('user', 't3', 'm3'), content: '', meta: { mid: 'm3', steer: true, steerState: 'retracted' } },
      msg('assistant', 't4', 'm4'),
    ]
    const ref = { current: messages }
    const { result } = renderHook(() => useRegenerate({
      dispatch: dispatch as never,
      activeSlot: 'slot-a',
      messages,
      messagesRef: ref,
      slotRunning: false,
      activeSlotRemoteBound: false,
      isStreaming: false,
      setRefusedPress: vi.fn(),
      showRefusedPress: vi.fn(),
    }))

    act(() => { result.current.handleRegenerate() })

    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'chat/truncateAfterIndex', payload: 1 }))
    expect(calls.regenerateSlot).toHaveBeenCalledWith('slot-a')
  })

  it('useTranscriptPins: handleJumpToQuote is stable and finds a row only the newest frame holds', () => {
    const navToDisplayIndex = vi.fn()
    const ref = { current: first }
    // Every non-row prop keeps one identity across renders, as ChatPage's do:
    // only `messages` moves between the two renders.
    const stable = {
      embedded: false, dispatch: vi.fn() as never, isMobile: false, activeSlot: 'slot-a',
      slotRunning: false, splitMode: false, crewWindow: null, slotHasMore: false, slotOldestIndex: 0,
      cursorIsForActiveSlot: true, loadingOlder: false, activeViewIsBoundedPage: false, isWelcomeState: false, activeSlotRemoteBound: false,
      scrollerRef: { current: null }, search: { isOpen: false } as never, navToDisplayIndex, pinnedJumpChrome: () => 0,
      scrollToPinnedPrompt: vi.fn(), setHighlightTs: vi.fn(), splitFeatureEnabled: false, regenerating: false, setEditLast: vi.fn(),
      editSeqRef: { current: 0 },
    }
    const build = (messages: ChatMessage[]) => ({
      ...stable, messages, messagesRef: latest(ref, messages),
      renderedDisplayItems: rows(messages), renderedTranscript: { messages, displayItems: rows(messages) },
    })
    const { result, rerender } = renderHook((p: { messages: ChatMessage[] }) => useTranscriptPins(build(p.messages) as never), {
      initialProps: { messages: first }, wrapper,
    })
    const before = result.current.handleJumpToQuote
    rerender({ messages: next })
    expect(result.current.handleJumpToQuote).toBe(before)
    act(() => { result.current.handleJumpToQuote({ ts: 't4', mid: 'm4' } as never) })
    expect(navToDisplayIndex).toHaveBeenCalledWith(3, expect.anything())
  })

  it('useMessageActions: handleEditResend is stable and snapshots the newest rows', () => {
    const base = {
      dispatch: vi.fn() as never, navigate: vi.fn() as never, filteredSlotsRef: { current: [] }, connected: true, activeSlot: 'slot-a',
      slotRunning: false, activityOpen: false, activeSlotRef: { current: 'slot-a' }, activeSlotRemoteBound: false,
      showActionError: vi.fn(), handleFileOpen: vi.fn() as never, handleFolderOpen: vi.fn(), search: { isOpen: false } as never,
      tabsCtl: {} as never, sessionTitles: new Map(), selectSessionTab: vi.fn(), linkPreviewsOn: false, planTaskId: '',
      setSidebarPinned: vi.fn(), sidebarAutoHidden: { current: null }, sidebarWidth: 280, setRegenerating: vi.fn(),
    }
    const ref = { current: first }
    const { result, rerender } = renderHook((p: { messages: ChatMessage[] }) => useMessageActions({ ...base, ...p, messagesRef: latest(ref, p.messages) }), {
      initialProps: { messages: first }, wrapper,
    })
    const before = result.current.handleEditResend
    rerender({ messages: next })
    expect(result.current.handleEditResend).toBe(before)
    act(() => { result.current.handleEditResend(2, 't3', 'edited') })
    expect(calls.rewindWithRollback).toHaveBeenCalledTimes(1)
    // The rollback restores the snapshot taken at press time: the newest frame's rows.
    const rollback = calls.rewindWithRollback.mock.calls[0][3] as () => void
    const dispatch = base.dispatch as unknown as ReturnType<typeof vi.fn>
    dispatch.mockClear()
    act(() => { rollback() })
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'chat/replaceMessages', payload: next }))
  })
})
