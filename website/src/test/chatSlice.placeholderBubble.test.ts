/**
 * Tests for placeholder bubble drop in _segment handlers.
 *
 * When the model emits trivially meaningless text ("...", "…", "..", etc.)
 * before a tool call, the _segment event should DROP the streaming message
 * entirely instead of converting it to an assistant message. This prevents
 * orphan "..." bubbles from appearing in the chat UI during sub-agent execution.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage } from '../store/chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'test-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const withSlot = { ...initial, activeSlot: SLOT }

// Helper: dispatch a chunk to create a streaming message
function chunk(state: ReturnType<typeof reducer>, content: string, seq = 1) {
  return reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content, seq }))
}

// Helper: dispatch a _segment
function segment(state: ReturnType<typeof reducer>) {
  return reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))
}

describe('_segment placeholder bubble drop (active slot)', () => {
  it('drops streaming message when content is "..."', () => {
    let state = chunk(withSlot, '...')
    expect(state.messages.some(m => m.role === 'streaming')).toBe(true)

    state = segment(state)

    // No streaming or assistant message should remain
    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is "…" (unicode ellipsis)', () => {
    let state = chunk(withSlot, '\u2026')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is ".."', () => {
    let state = chunk(withSlot, '..')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is "...."', () => {
    let state = chunk(withSlot, '....')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is empty', () => {
    // Edge case: empty streaming content should also be dropped
    let state = { ...withSlot, messages: [{ role: 'streaming', content: '', cls: 'msg msg-a' } as ChatMessage] }
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is "..." with whitespace', () => {
    let state = chunk(withSlot, '  ...  ')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is spaced dots ". . ."', () => {
    let state = chunk(withSlot, '. . .')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is dashes "---"', () => {
    let state = chunk(withSlot, '---')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message when content is em-dashes "——"', () => {
    let state = chunk(withSlot, '\u2014\u2014')
    state = segment(state)

    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(0)
  })

  it('does NOT drop single-char content like "-" (could be list marker)', () => {
    let state = chunk(withSlot, '-')
    state = segment(state)

    const assistants = state.messages.filter(m => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0].content).toBe('-')
  })

  it('keeps streaming message when content is meaningful', () => {
    let state = chunk(withSlot, 'Let me ')
    state = chunk(state, 'check that', 2)
    state = segment(state)

    // Should convert to assistant, NOT drop
    expect(state.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    const assistants = state.messages.filter(m => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0].content).toBe('Let me check that')
  })

  it('keeps streaming message when content has meaningful text with dots', () => {
    let state = chunk(withSlot, 'Searching...')
    state = segment(state)

    const assistants = state.messages.filter(m => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0].content).toBe('Searching...')
  })
})

describe('_segment placeholder bubble drop (non-active slot)', () => {
  const OTHER = 'other-slot'
  const bgState = { ...initial, activeSlot: OTHER }

  function bgChunk(state: ReturnType<typeof reducer>, content: string, seq = 1) {
    return reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content, seq }))
  }

  function bgSegment(state: ReturnType<typeof reducer>) {
    return reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))
  }

  it('drops streaming message in non-active slot when content is "..."', () => {
    let state = bgChunk(bgState, '...')
    const slotMsgs = state.slotMessages[SLOT] ?? []
    expect(slotMsgs.some((m: ChatMessage) => m.role === 'streaming')).toBe(true)

    state = bgSegment(state)

    const finalMsgs = state.slotMessages[SLOT] ?? []
    expect(finalMsgs.filter((m: ChatMessage) => m.role === 'streaming')).toHaveLength(0)
    expect(finalMsgs.filter((m: ChatMessage) => m.role === 'assistant')).toHaveLength(0)
  })

  it('drops streaming message in non-active slot when content is "…"', () => {
    let state = bgChunk(bgState, '\u2026')
    state = bgSegment(state)

    const finalMsgs = state.slotMessages[SLOT] ?? []
    expect(finalMsgs.filter((m: ChatMessage) => m.role === 'streaming')).toHaveLength(0)
    expect(finalMsgs.filter((m: ChatMessage) => m.role === 'assistant')).toHaveLength(0)
  })

  it('keeps meaningful content in non-active slot', () => {
    let state = bgChunk(bgState, 'Hello world')
    state = bgSegment(state)

    const finalMsgs = state.slotMessages[SLOT] ?? []
    const assistants = finalMsgs.filter((m: ChatMessage) => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0].content).toBe('Hello world')
  })
})
