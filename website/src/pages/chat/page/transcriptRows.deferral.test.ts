import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import type { ChatMessage } from '../../../types'
import type { DisplayItem } from '../types'

/**
 * What the transcript shows while React cannot finish a background render.
 *
 * A streaming reply re-renders the chat page at urgent priority on every flush,
 * and each urgent render restarts whatever the transcript's deferral has
 * pending. On a device that cannot finish that render between two flushes, the
 * deferred value stays at its last committed frame for the whole reply.
 * `useDeferredValue` is replaced with a stand-in that holds exactly that state,
 * so each test states what must be on screen without depending on how fast the
 * machine running it is.
 */

// Holds the deferred value at the first frame the hook hands in, the way React
// keeps the last committed one while every background render is interrupted.
let frozen: { set: boolean; value: unknown } = { set: false, value: undefined }
/** Every input handed to useDeferredValue, in render order. */
const inputs: unknown[] = []
vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>()
  return {
    ...actual,
    useDeferredValue: <T,>(value: T): T => {
      inputs.push(value)
      if (!frozen.set) frozen = { set: true, value }
      return frozen.value as T
    },
  }
})

import { useTranscriptRows } from './transcriptRows'

afterEach(() => {
  frozen = { set: false, value: undefined }
  inputs.length = 0
})

const msg = (role: string, content: string, ts: string) => ({ role, content, ts, cls: '' }) as ChatMessage
const history = [msg('user', 'earlier', 't0'), msg('assistant', 'a', 'ta'), msg('tool', 't', 'tb'), msg('assistant', 'b', 'tc')]
const prompt = msg('user', 'question', 't1')
const NO_APPS: ReadonlySet<string> = new Set()

function mount(slot: string, messages: ChatMessage[]) {
  const displayItemsRef = { current: [] as DisplayItem[] }
  const scrollerRef = { current: null }
  return renderHook(
    ({ slot, messages }) => useTranscriptRows({
      messages, slotRunning: true, activeSlot: slot, appToolCallIds: NO_APPS, displayItemsRef, scrollerRef,
    }),
    { initialProps: { slot, messages } },
  )
}

describe('useTranscriptRows while the background render cannot finish', () => {
  it('draws the sent prompt, the reply and a tool row in the render that brings them', () => {
    const { result, rerender } = mount('chat-a', history)
    const shown = () => result.current.renderedTranscript.messages
    const steps = [
      [...history, prompt],
      [...history, prompt, msg('streaming', 'ans', 't2')],
      [...history, prompt, msg('streaming', 'answer', 't2')],
      [...history, prompt, msg('assistant', 'answer', 't2'), msg('tool', 'ran', 't3')],
      [...history, prompt, msg('assistant', 'answer', 't2'), msg('tool', 'ran', 't3'), msg('streaming', 'done', 't4')],
    ]
    for (const messages of steps) {
      rerender({ slot: 'chat-a', messages })
      expect(shown()).toBe(messages)
      // The rows the virtualizer draws come from the same frame.
      expect(result.current.renderedDisplayItems).toBe(result.current.renderedTranscript.displayItems)
    }
    // The deferral was handed one frame for the whole turn, so React had no
    // background render to run and nothing for the next flush to interrupt.
    expect(new Set(inputs).size).toBe(1)
  })

  it('keeps a page of older rows deferred, with the reply as far as it was drawn', () => {
    const { result, rerender } = mount('chat-a', history)
    const live = [...history, prompt, msg('streaming', 'ans', 't2')]
    rerender({ slot: 'chat-a', messages: [...history, prompt] })
    rerender({ slot: 'chat-a', messages: live })
    const older = [msg('user', 'oldest', 't-2'), msg('assistant', 'old', 't-1')]
    rerender({ slot: 'chat-a', messages: [...older, ...live] })
    // The landed page waits for its background render; the reply does not roll
    // back to the frame the deferral holds.
    expect(result.current.renderedTranscript.messages).toBe(live)
  })

  it('keeps the first rows of an empty transcript deferred', () => {
    const empty: ChatMessage[] = []
    const { result, rerender } = mount('chat-a', empty)
    rerender({ slot: 'chat-a', messages: history })
    expect(result.current.renderedTranscript.messages).toBe(empty)
  })

  it('shows the incoming session at once on a switch, never rows of the one left behind', () => {
    const { result, rerender } = mount('chat-a', history)
    rerender({ slot: 'chat-a', messages: [...history, prompt] })
    // Another session whose transcript starts with the same rows and is longer:
    // judged by shape alone it would read as rows added at the tail.
    const other = [...history, msg('user', 'other session', 'tx'), msg('assistant', 'its reply', 'ty')]
    rerender({ slot: 'chat-b', messages: other })
    expect(result.current.renderedTranscript.messages).toBe(other)
    // Back to the first session, which moved on meanwhile.
    const back = [...history, prompt, msg('assistant', 'answer', 't2')]
    rerender({ slot: 'chat-a', messages: back })
    expect(result.current.renderedTranscript.messages).toBe(back)
  })
})
