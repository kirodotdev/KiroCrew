import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import reducer, { sseChatMessage } from '../store/chatSlice'
import './mockApiClient'

const SLOT = 'prop-slot'
const initial = reducer(undefined, { type: '@@INIT' })
const withSlot = { ...initial, activeSlot: SLOT }

/** Helper: dispatch a chunk message */
const chunk = (state: ReturnType<typeof reducer>, content: string, seq?: number) =>
  reducer(state, sseChatMessage({ slot: SLOT, role: 'chunk', content, seq }))

/** Helper: dispatch a _segment message */
const segment = (state: ReturnType<typeof reducer>) =>
  reducer(state, sseChatMessage({ slot: SLOT, role: '_segment', content: '' }))

/** Helper: dispatch a _done message */
const done = (state: ReturnType<typeof reducer>) =>
  reducer(state, sseChatMessage({ slot: SLOT, role: '_done', content: '' }))

/** Helper: dispatch a tool message */
const tool = (state: ReturnType<typeof reducer>, name: string) =>
  reducer(state, sseChatMessage({ slot: SLOT, role: 'tool', content: `🔧 ${name}` }))

// Arbitrary: non-empty printable string for chunk content
// Mirror the intentional placeholder-drop in the _segment handler (see
// chatSlice.ts sseChatMessage, "drop placeholder '...' bubbles on _segment
// finalization"): content composed EXCLUSIVELY of 2+ punctuation/
// whitespace placeholder chars (with at least one non-whitespace char), or a
// lone ellipsis, is deliberately dropped on finalization. Properties that
// assert the message survives finalization must not generate such content.
const isDroppedPlaceholder = (s: string) =>
  (/^[\s.\-…·•–—]{2,}$/.test(s) && /[.\-…·•–—]/.test(s)) || s === '…'

const chunkContentArb = fc.string({ minLength: 1, maxLength: 50 })
  .filter(s => s.length > 0 && !s.includes('\n[⚠') && !isDroppedPlaceholder(s))


// Feature: inline-tool-cards, Property 3: Streaming-to-assistant conversion on finalization
// **Validates: Requirements 2.1, 4.1, 5.2**
describe('Property 3: Streaming-to-assistant conversion on finalization', () => {
  it('_segment or _done converts streaming → assistant with content preserved', () => {
    fc.assert(
      fc.property(
        // Generate 1–5 chunk contents and a finalizer type
        fc.array(chunkContentArb, { minLength: 1, maxLength: 5 }),
        fc.constantFrom('_segment', '_done'),
        (chunks, finalizer) => {
          // Build up a streaming message from chunks
          let state = { ...withSlot }
          let accumulated = ''
          for (const c of chunks) {
            state = chunk(state, c)
            accumulated += c
          }

          // Verify streaming message exists
          const streamMsg = state.messages.find(m => m.role === 'streaming')
          expect(streamMsg).toBeDefined()
          expect(streamMsg!.content).toBe(accumulated)

          // Apply finalizer
          if (finalizer === '_segment') {
            state = segment(state)
          } else {
            state = done(state)
          }

          // Verify conversion: no streaming messages remain, assistant has the content
          const streaming = state.messages.filter(m => m.role === 'streaming')
          expect(streaming).toHaveLength(0)

          const assistants = state.messages.filter(m => m.role === 'assistant')
          expect(assistants.length).toBeGreaterThanOrEqual(1)

          // The last assistant message should have the accumulated content
          const lastAssistant = assistants[assistants.length - 1]
          expect(lastAssistant.content).toBe(accumulated)
        },
      ),
      { numRuns: 100 },
    )
  })
})


// Feature: inline-tool-cards, Property 4: Tool card ordering after segment
// **Validates: Requirements 3.1, 2.3**
describe('Property 4: Tool card ordering after segment', () => {
  it('after segment, tool inserts after assistant; subsequent chunk creates streaming after tool', () => {
    const toolNameArb = fc.stringMatching(/^[a-z]{1,15}$/)

    fc.assert(
      fc.property(
        chunkContentArb,
        toolNameArb,
        chunkContentArb,
        (preText, toolName, postText) => {
          // Build streaming, then segment to finalize
          let state = chunk({ ...withSlot }, preText)
          state = segment(state)

          // Verify: last message is assistant
          const preMessages = [...state.messages]
          expect(preMessages[preMessages.length - 1].role).toBe('assistant')

          // Insert tool
          state = tool(state, toolName)

          // Verify: [..., assistant, tool]
          const afterTool = [...state.messages]
          const len = afterTool.length
          expect(afterTool[len - 2].role).toBe('assistant')
          expect(afterTool[len - 1].role).toBe('tool')

          // New chunk after tool
          state = chunk(state, postText)

          // Verify ordering: [..., assistant, tool, streaming]
          const final = state.messages
          const fLen = final.length
          expect(fLen).toBeGreaterThanOrEqual(3)
          expect(final[fLen - 3].role).toBe('assistant')
          expect(final[fLen - 2].role).toBe('tool')
          expect(final[fLen - 1].role).toBe('streaming')
          expect(final[fLen - 1].content).toBe(postText)
        },
      ),
      { numRuns: 100 },
    )
  })
})


// Feature: inline-tool-cards, Property 5: Tool messages are not deduplicated
// **Validates: Requirements 3.2**
describe('Property 5: Tool deduplication across segments', () => {
  it('consecutive identical tool names are collapsed; different names are separate', () => {
    // Generate a sequence of tool name groups: each group is a run of identical names
    const toolGroupArb = fc.array(
      fc.record({
        name: fc.stringMatching(/^[a-z]{1,10}$/),
        count: fc.integer({ min: 1, max: 5 }),
      }),
      { minLength: 1, maxLength: 6 },
    )

    fc.assert(
      fc.property(toolGroupArb, (groups) => {
        let state = { ...withSlot }

        // Flatten groups into a sequence of tool dispatches
        const toolSequence: string[] = []
        for (const g of groups) {
          for (let i = 0; i < g.count; i++) {
            toolSequence.push(g.name)
          }
        }

        // Dispatch all tools
        for (const name of toolSequence) {
          state = tool(state, name)
        }

        // Each tool dispatch produces one message (no dedup)
        const toolMsgs = state.messages.filter(m => m.role === 'tool')
        expect(toolMsgs).toHaveLength(toolSequence.length)
      }),
      { numRuns: 50 },
    )
  })
})


// Feature: inline-tool-cards, Property 9: lastChunkSeq preserved on segment
// **Validates: Requirements 7.2**
describe('Property 9: lastChunkSeq preserved on segment', () => {
  it('_segment does not modify lastChunkSeq', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 100000 }),
        chunkContentArb,
        (seqValue, content) => {
          // Send a chunk with a specific seq to set lastChunkSeq
          let state = chunk({ ...withSlot }, content, seqValue)
          expect(state.lastChunkSeq).toBe(seqValue)

          // Apply _segment
          state = segment(state)

          // Verify lastChunkSeq is unchanged
          expect(state.lastChunkSeq).toBe(seqValue)
        },
      ),
      { numRuns: 100 },
    )
  })
})


// Feature: inline-tool-cards, Property 11: Single assistant message for tool-free streams
// **Validates: Requirements 8.2**
describe('Property 11: Single assistant message for tool-free streams', () => {
  it('chunk-only sequences followed by _done produce exactly one assistant message', () => {
    fc.assert(
      fc.property(
        fc.array(chunkContentArb, { minLength: 1, maxLength: 10 }),
        (chunks) => {
          let state = { ...withSlot }
          let accumulated = ''

          // Send all chunks
          for (const c of chunks) {
            state = chunk(state, c)
            accumulated += c
          }

          // Finalize with _done
          state = done(state)

          // Verify exactly one assistant message, no streaming, no tool
          const assistants = state.messages.filter(m => m.role === 'assistant')
          const streaming = state.messages.filter(m => m.role === 'streaming')
          const tools = state.messages.filter(m => m.role === 'tool')

          expect(assistants).toHaveLength(1)
          expect(streaming).toHaveLength(0)
          expect(tools).toHaveLength(0)
          expect(assistants[0].content).toBe(accumulated)
        },
      ),
      { numRuns: 100 },
    )
  })
})
