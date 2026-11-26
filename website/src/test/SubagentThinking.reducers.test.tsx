/**
 * A sub-agent's reasoning (`subagent_thinking`) lands on its card's
 * `thinking`, apart from the card's Output (`streaming`):
 *  - a streamFlush subagents item with `thinking` appends to it;
 *  - a text-only item leaves it absent;
 *  - past SUBAGENT_THINKING_CAP only the newest SUBAGENT_THINKING_KEEP stay,
 *    behind the truncation marker;
 *  - the spawn and snapshot rebuilds keep it (no frame carries it);
 *  - an unsafe key creates nothing.
 */
import { describe, it, expect } from 'vitest'
import { createTestStore } from './helpers'
import { setActiveSlot, sseSubagentSpawn, sseSubagentSnapshot, sseSubagentBatchChunks, SUBAGENT_THINKING_CAP, SUBAGENT_THINKING_KEEP } from '../store/chatSlice'
import { streamFlush } from '../store/streamFlush'
import { i18nT } from '../i18n/t'

const CARD = 'a1b2c3d4'

function spawned() {
  const store = createTestStore()
  // A snapshot needs an owning slot: one with none is refused as an orphan.
  const slot = 'chat-thinking'
  store.dispatch(setActiveSlot(slot))
  store.dispatch(sseSubagentSpawn({ slot, id: CARD, task: 'solve the puzzle', agent: 'general-purpose' }))
  return { store, slot }
}

const flush = (subagents: { id: string; slot: string; text: string; thinking?: string }[]) =>
  streamFlush({ slots: [], subagents, recency: [] })

describe('subagent thinking reducers', () => {
  it('appends a flush item\'s thinking and leaves the output untouched', () => {
    const { store, slot } = spawned()
    store.dispatch(flush([{ id: CARD, slot, text: '', thinking: 'Dave is in seat 1. ' }]))
    store.dispatch(flush([{ id: CARD, slot, text: 'Alice', thinking: 'Eve sits in seat 2.' }]))
    const card = store.getState().chat.subagents[CARD]
    expect(card.thinking).toBe('Dave is in seat 1. Eve sits in seat 2.')
    expect(card.streaming).toBe('Alice')
  })

  it('a text-only item adds no thinking', () => {
    const { store, slot } = spawned()
    store.dispatch(flush([{ id: CARD, slot, text: 'output' }]))
    const card = store.getState().chat.subagents[CARD]
    expect(card.streaming).toBe('output')
    expect(card.thinking).toBeUndefined()
  })

  it('keeps only the newest reasoning past the cap, behind the marker', () => {
    const { store, slot } = spawned()
    const head = 'a'.repeat(SUBAGENT_THINKING_CAP)
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ id: CARD, slot, text: '', thinking: head }] }))
    expect(store.getState().chat.subagents[CARD].thinking).toBe(head)
    store.dispatch(sseSubagentBatchChunks({ chunks: [{ id: CARD, slot, text: '', thinking: 'zz' }] }))
    const thinking = store.getState().chat.subagents[CARD].thinking as string
    const marker = i18nT('store.chatSlice.truncated') + '\n'
    expect(thinking.startsWith(marker)).toBe(true)
    expect(thinking.slice(marker.length)).toHaveLength(SUBAGENT_THINKING_KEEP)
    expect(thinking.endsWith('zz')).toBe(true)
  })

  it('the spawn and snapshot rebuilds keep the reasoning', () => {
    const { store, slot } = spawned()
    store.dispatch(flush([{ id: CARD, slot, text: '', thinking: 'kept' }]))
    store.dispatch(sseSubagentSpawn({ slot, id: CARD, task: 'solve the puzzle', agent: 'general-purpose' }))
    expect(store.getState().chat.subagents[CARD].thinking).toBe('kept')
    store.dispatch(sseSubagentSnapshot({
      id: CARD, slot, task: 'solve the puzzle', agent: 'general-purpose', streaming: 'out', last_tool: '', started: 1,
    }))
    const card = store.getState().chat.subagents[CARD]
    expect(card.thinking).toBe('kept')
    expect(card.streaming).toBe('out')
  })

  it('an unsafe key creates nothing', () => {
    const { store, slot } = spawned()
    store.dispatch(flush([{ id: '__proto__', slot, text: '', thinking: 'evil' }]))
    expect(Object.keys(store.getState().chat.subagents)).toEqual([CARD])
    expect(({} as Record<string, unknown>).thinking).toBeUndefined()
  })
})
