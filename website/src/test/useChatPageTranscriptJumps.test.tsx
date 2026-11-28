/**
 * Message-targeted scroll intent (pages/chat/page/transcriptJumps.ts), driven as
 * a hook against a spy for the transcript's `navToMessage`, which records the
 * display row each jump was for.
 *
 * Pins the parts ChatPage-level suites never reach:
 *  - the approval bar's "Show in chat" scrolls to the tool row by call id;
 *  - a message with no row on screen is never scrolled to.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'

import { useTranscriptJumps } from '../pages/chat/page/transcriptJumps'
import { openActivityToTool } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { createTestStore } from './helpers'

type Opts = Parameters<typeof useTranscriptJumps>[0]

function harness(over: Partial<Opts> = {}) {
  const store = createTestStore()
  const map = new Map<number, number>([[0, 10], [2, 12], [3, 13]])
  const messages = [
    { role: 'user', content: 'a' },
    { role: 'assistant', content: 'b' },
    { role: 'assistant', content: 'c' },
    { role: 'tool', content: 'd', meta: { tool_call_id: 'call-9' } },
  ] as ChatMessage[]
  // The rows on screen, as the render being driven has them: message index to
  // display row through `messageToDisplayIdx`, the way the page's rows draw them.
  const live = { messages, map }
  const rowOfMessage = (m: ChatMessage) => live.map.get(live.messages.indexOf(m)) ?? -1
  /** The display row of each jump that found one, and its options. */
  const jumped: Array<[number, unknown]> = []
  const navToMessage = vi.fn((m: ChatMessage, o?: unknown) => {
    const di = rowOfMessage(m)
    if (di < 0) return false
    jumped.push([di, o])
    return true
  })
  const opts: Opts = {
    messages,
    messageToDisplayIdx: map,
    navToMessage,
    rowOfMessage,
    activeSlot: 'slot-a',
    cursorIsForActiveSlot: true,
    slotHasMore: false,
    slotOldestIndex: 0,
    initialMsgRef: { current: null },
    initialMidRef: { current: null },
    initialSidRef: { current: null },
    markJump: vi.fn(),
    handleJumpToPinnedMessage: vi.fn(),
    ...over,
  }
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook((p: Opts) => {
    live.messages = p.messages
    live.map = p.messageToDisplayIdx
    return useTranscriptJumps(p)
  }, { initialProps: opts, wrapper })
  return { opts, hook, store, jumped }
}

describe('"Show in chat" from the approval bar', () => {
  it('scrolls smoothly to the tool row that carries the focused call id', () => {
    const { store, jumped } = harness()
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(jumped).toEqual([[13, { behavior: 'smooth', align: 'center' }]])
  })

  it('ignores a call id that no loaded tool row carries', () => {
    const { opts, store } = harness()
    act(() => { store.dispatch(openActivityToTool('call-missing')) })
    expect(opts.navToMessage).not.toHaveBeenCalled()
  })

  it('scrolls to a tool row the rows on screen do not draw yet once they draw it', () => {
    const { opts, hook, store, jumped } = harness({ messageToDisplayIdx: new Map([[0, 10]]) })
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(jumped).toEqual([])
    hook.rerender({ ...opts, messageToDisplayIdx: new Map([[0, 10], [3, 13]]) })
    expect(jumped).toEqual([[13, { behavior: 'smooth', align: 'center' }]])
  })
})

describe('a ?msg= link', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('waits while the rows on screen do not draw the linked message, then lands on it', () => {
    const messages = [
      { role: 'user', content: 'a', ts: 't0' },
      { role: 'assistant', content: 'b', ts: 't1' },
    ] as ChatMessage[]
    const { opts, hook, jumped } = harness({ messages, messageToDisplayIdx: new Map([[0, 10]]), initialMsgRef: { current: 't1' } })
    // The link scrolls 500ms after its row is found.
    act(() => { vi.advanceTimersByTime(600) })
    expect(jumped).toEqual([])
    expect(opts.markJump).not.toHaveBeenCalled()
    expect(opts.initialMsgRef.current).toBe('t1')

    hook.rerender({ ...opts, messageToDisplayIdx: new Map([[0, 10], [1, 11]]) })
    act(() => { vi.advanceTimersByTime(600) })
    expect(jumped).toEqual([[11, { behavior: 'auto', align: 'center' }]])
    expect(opts.markJump).toHaveBeenCalledWith('t1', true, undefined)
  })
})
