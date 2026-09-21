/** A queued send's `rawSend` may only be synthesized from the optimistic bubble when that bubble is
 *  the WHOLE sent payload.
 *
 *  A knowledge send's wire text is `expandKnowledgeBlock(block) + '\n' + typed`, while the bubble
 *  holds `displayTxt` -- the typed half alone. Synthesizing `rawSend` from the bubble therefore hands
 *  a cancel back the prompt with its selected context silently gone, AND its presence suppresses the
 *  `restoreQueuedContent` fallback, which reads the SERVER's content and so still carries the block.
 *  `stashIsLossless` (ChatPage) already gates the pre-send stash on the same distinction.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, appendMessage, appendQueuedMessage } from './chatSlice'

const SLOT = 'slot-knowledge'
const TYPED = 'summarize the runbook for me'

type Row = { role: string; content: string; meta?: Record<string, unknown> }
type State = { chat: { messages: Row[] } }

describe('a queued knowledge send keeps its selected context', () => {
  let store: ReturnType<typeof configureStore>
  const queuedRow = () => (store.getState() as State).chat.messages.find(m => m.role === 'queued')

  const seed = (sendId: string, meta: Record<string, unknown>) => {
    store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot(SLOT))
    store.dispatch(appendMessage({
      role: 'user', content: TYPED, cls: '',
      meta: { sendId, optimistic: true, pendingServerRow: true, ...meta },
    } as never))
    store.dispatch(appendQueuedMessage({
      slot: SLOT, content: '[redacted]', ts: new Date().toISOString(), queueId: 'ignored', sendId,
    }))
  }

  beforeEach(() => { store = configureStore({ reducer: { chat: chatReducer } }) })

  it('synthesizes NO rawSend from a bubble that omits the prepended knowledge block', () => {
    seed('s-knowledge', {
      knowledge: { items: 2, tokens: 900, titles: ['Runbook', 'Alarms'], content: [{ title: 'Runbook', text: 'step one' }] },
    })
    const row = queuedRow()
    expect(row, 'premise: the queue push produced a card').toBeTruthy()
    expect(row?.meta?.rawSend,
      'a rawSend built from the bubble would drop the context and suppress the parser fallback',
    ).toBeUndefined()
  })

  it('still synthesizes rawSend for an ordinary send, whose bubble IS the payload', () => {
    // Positive control: skipping unconditionally would satisfy the assertion above while throwing
    // away the one copy of the typed text a redacted queue card cannot reconstruct.
    seed('s-plain', {})
    expect((queuedRow()?.meta?.rawSend as { text?: string } | undefined)?.text).toBe(TYPED)
  })
})
