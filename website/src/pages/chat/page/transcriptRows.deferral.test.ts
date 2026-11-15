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
/** React finds room and commits the background render: the deferred value catches up. */
const backgroundRenderLands = () => { frozen = { set: true, value: inputs[inputs.length - 1] } }
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

function mount(slot: string, messages: ChatMessage[], loaded = true) {
  const displayItemsRef = { current: [] as DisplayItem[] }
  const scrollerRef = { current: null }
  return renderHook(
    ({ slot, messages, loaded = true }: { slot: string; messages: ChatMessage[]; loaded?: boolean }) => useTranscriptRows({
      messages, transcriptLoaded: loaded, slotRunning: true, activeSlot: slot, appToolCallIds: NO_APPS, displayItemsRef, scrollerRef,
    }),
    { initialProps: { slot, messages, loaded } },
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

  it('keeps the first rows of a transcript that is still loading deferred', () => {
    const empty: ChatMessage[] = []
    const { result, rerender } = mount('chat-a', empty, false)
    rerender({ slot: 'chat-a', messages: history, loaded: true })
    expect(result.current.renderedTranscript.messages).toBe(empty)
  })

  it('draws the first send into an empty chat, and its reply, in the render that brings them', () => {
    const { result, rerender } = mount('chat-a', [])
    const shown = () => result.current.renderedTranscript.messages
    const steps = [
      [prompt],
      [prompt, msg('streaming', 'ans', 't2')],
      [prompt, msg('streaming', 'answer', 't2')],
      [prompt, msg('assistant', 'answer', 't2'), msg('tool', 'ran', 't3')],
      [prompt, msg('assistant', 'answer', 't2'), msg('tool', 'ran', 't3'), msg('streaming', 'done', 't4')],
    ]
    for (const messages of steps) {
      rerender({ slot: 'chat-a', messages })
      expect(shown()).toBe(messages)
      expect(result.current.renderedDisplayItems).toBe(result.current.renderedTranscript.displayItems)
    }
    // The deferral took the first rows, once: what it holds has moved on from
    // the empty chat, and the rest of the turn handed it nothing more.
    expect(new Set(inputs).size).toBe(2)
  })

  it('draws the first send at once in a chat that loaded empty', () => {
    const empty: ChatMessage[] = []
    const { result, rerender } = mount('chat-a', empty, false)
    // The page lands with no rows. The deferral is told, and that render has
    // nothing to draw, so React commits it while the user types the prompt.
    rerender({ slot: 'chat-a', messages: empty, loaded: true })
    backgroundRenderLands()
    const sent = [prompt]
    rerender({ slot: 'chat-a', messages: sent })
    expect(result.current.renderedTranscript.messages).toBe(sent)
    const replying = [prompt, msg('streaming', 'ans', 't2')]
    rerender({ slot: 'chat-a', messages: replying })
    expect(result.current.renderedTranscript.messages).toBe(replying)
  })

  it('keeps a page of several turns deferred when it lands in a loaded empty chat', () => {
    const empty: ChatMessage[] = []
    const { result, rerender } = mount('chat-a', empty)
    const page = [...history, prompt, msg('assistant', 'answer', 't2')]
    rerender({ slot: 'chat-a', messages: page })
    expect(result.current.renderedTranscript.messages).toBe(empty)
  })

  it('keeps a page deferred when it lands in an empty chat that started loading again', () => {
    const empty: ChatMessage[] = []
    const { result, rerender } = mount('chat-a', empty)
    rerender({ slot: 'chat-a', messages: empty, loaded: false })
    backgroundRenderLands()
    // One turn, so only the loading mark tells this page from a first send.
    rerender({ slot: 'chat-a', messages: history, loaded: true })
    expect(result.current.renderedTranscript.messages).toBe(empty)
  })

  it('keeps the first turn on screen when a second one starts before its rows committed, then shows both', () => {
    const { result, rerender } = mount('chat-a', [])
    rerender({ slot: 'chat-a', messages: [prompt] })
    const replied = [prompt, msg('streaming', 'answer', 't2')]
    rerender({ slot: 'chat-a', messages: replied })
    const second = [prompt, msg('assistant', 'answer', 't2'), msg('user', 'and then?', 't3')]
    rerender({ slot: 'chat-a', messages: second })
    // Not the empty chat the deferral last committed: the reply stays up.
    expect(result.current.renderedTranscript.messages).toBe(replied)
    backgroundRenderLands()
    rerender({ slot: 'chat-a', messages: second })
    expect(result.current.renderedTranscript.messages).toBe(second)
  })

  it('never shows the session left behind on the first send of a new chat', () => {
    const { result, rerender } = mount('chat-a', history)
    const empty: ChatMessage[] = []
    rerender({ slot: 'chat-new', messages: empty })
    expect(result.current.renderedTranscript.messages).toBe(empty)
    const sent = [prompt]
    rerender({ slot: 'chat-new', messages: sent })
    expect(result.current.renderedTranscript.messages).toBe(sent)
    const replying = [prompt, msg('streaming', 'ans', 't2')]
    rerender({ slot: 'chat-new', messages: replying })
    expect(result.current.renderedTranscript.messages).toBe(replying)
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
