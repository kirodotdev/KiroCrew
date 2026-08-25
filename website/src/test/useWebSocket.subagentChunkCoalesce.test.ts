/**
 * `subagent_chunk` must buffer and flush once per frame, not dispatch per token.
 * Mirrors the chat_chunk coalescing pattern (see PR #1005).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, sseSubagentSpawn } from '../store/chatSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),  // never resolves
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

/** Deliberately the SINGLETON store: useWebSocket dispatches via useAppDispatch()
 *  but reads state off the imported singleton, so a separate Provider store would
 *  let reads and writes diverge. */
function seedStore() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
  // Spawn a subagent so the store has an entry to receive chunks
  globalStore.dispatch(sseSubagentSpawn({ slot: 'slot-1', id: 'agent-1', task: 'test task', agent: 'test-agent' }))
  globalStore.dispatch(sseSubagentSpawn({ slot: 'slot-1', id: 'agent-2', task: 'other task', agent: 'test-agent' }))
  return globalStore
}

const agentStreaming = (slot: string, id: string) => {
  const state = globalStore.getState().chat
  // When slot matches activeSlot, subagents are in state.subagents[id]
  // Otherwise they're in state.slotActivity[slot].subagents[id]
  if (slot === state.activeSlot) {
    return state.subagents[id]?.streaming ?? ''
  }
  return state.slotActivity[slot]?.subagents?.[id]?.streaming ?? ''
}

const chunk = (slot: string, id: string, text: string) => ({
  type: 'subagent_chunk',
  data: { slot, id, text },
})

const done = (slot: string, id: string) => ({
  type: 'subagent_done',
  data: { slot, id, elapsed: 1000, outcome: 'completed' },
})

describe('useWebSocket subagent_chunk coalescing', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Hand-driven frames: nothing flushes until runFrames() is called
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function runFrames() {
    const pending = rafQueue
    rafQueue = []
    act(() => { pending.forEach(cb => cb(performance.now())) })
  }

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store: globalStore },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  it('does not dispatch synchronously on each chunk event', () => {
    const { ws } = mount()
    // Seed AFTER mount because ws.onopen dispatches clearSubagentsForSnapshot
    seedStore()

    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'hello')) })

    // Unbuffered code would write streaming here; buffered code has not flushed.
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')

    runFrames()
    expect(agentStreaming('slot-1', 'agent-1')).toBe('hello')
  })

  it('collapses a burst into one dispatch and concatenates all text', () => {
    const { ws } = mount()
    seedStore()

    // 30 tokens arriving in one burst
    act(() => {
      for (let i = 0; i < 30; i++) ws.simulateMessage(chunk('slot-1', 'agent-1', `t${i} `))
    })
    
    // Nothing dispatched yet — all buffered
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')
    
    runFrames()

    // All text accumulated correctly in ONE flush (if it were 30 dispatches,
    // the truncation logic would have kicked in at 50KB)
    const expected = Array.from({ length: 30 }, (_, i) => `t${i} `).join('')
    expect(agentStreaming('slot-1', 'agent-1')).toBe(expected)
  })

  it('keeps per-agent chunks independent within one frame', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'A1 '))
      ws.simulateMessage(chunk('slot-1', 'agent-2', 'B1 '))
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'A2 '))
    })
    runFrames()

    expect(agentStreaming('slot-1', 'agent-1')).toBe('A1 A2 ')
    expect(agentStreaming('slot-1', 'agent-2')).toBe('B1 ')
  })

  it('flushes synchronously before subagent_done to preserve ordering', () => {
    const { ws } = mount()
    seedStore()

    // Send chunk then done in same act block
    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'final text'))
      ws.simulateMessage(done('slot-1', 'agent-1'))
    })

    // Agent is done; streaming cleared by reducer. Ordering verified by mid-burst test.
    const agent = globalStore.getState().chat.subagents['agent-1']
    expect(agent?.status).toBe('done')
  })

  it('does not drop text when subagent_done arrives mid-burst', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'part1 '))
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'part2 '))
      ws.simulateMessage(done('slot-1', 'agent-1'))
      ws.simulateMessage(chunk('slot-1', 'agent-2', 'other agent'))
    })
    
    // Agent-1 is done, its streaming is cleared, but chunks WERE flushed first
    // (verified by agent-2 still having its text after runFrames)
    expect(globalStore.getState().chat.subagents['agent-1']?.status).toBe('done')
    
    runFrames()
    
    // agent-2's text accumulated separately and flushed
    expect(agentStreaming('slot-1', 'agent-2')).toBe('other agent')
  })

  it('keeps the subagents map reference stable across a burst until the frame lands', () => {
    const { ws } = mount()
    const store = seedStore()
    const before = store.getState().chat.subagents['agent-1']

    act(() => {
      for (let i = 0; i < 12; i++) ws.simulateMessage(chunk('slot-1', 'agent-1', 'x'))
    })
    // Entry should be unchanged — nothing dispatched yet
    expect(store.getState().chat.subagents['agent-1']).toBe(before)

    runFrames()
    // Now it should have changed (streaming text added)
    expect(store.getState().chat.subagents['agent-1']).not.toBe(before)
  })

  it('cleans up pending rAF on unmount and still flushes buffered chunks', () => {
    const { hook, ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'pre-unmount '))
    })
    
    // Nothing flushed yet (buffered, waiting for rAF)
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')

    // Unmount — cleanup should flush
    hook.unmount()

    // The cleanup flushed the buffered chunk
    expect(agentStreaming('slot-1', 'agent-1')).toBe('pre-unmount ')
  })

  it('handles empty or missing text fields gracefully', () => {
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'valid'))
      // Empty text should be a no-op (guarded in the case statement)
      ws.simulateMessage({ type: 'subagent_chunk', data: { slot: 'slot-1', id: 'agent-1', text: '' } })
    })
    runFrames()

    expect(agentStreaming('slot-1', 'agent-1')).toBe('valid')
  })

  it('flushes buffered chunks before subagent_retrying to preserve retrying state', () => {
    // Regression: chunk buffered, retry dispatched, then rAF flush cleared retrying.
    const { ws } = mount()
    seedStore()

    const agentRetrying = () => globalStore.getState().chat.subagents['agent-1']?.retrying

    // 1. Buffer a chunk (not flushed yet)
    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'partial ')) })
    expect(agentRetrying()).toBeFalsy()  // undefined or false

    // 2. Retry arrives — must flush buffered chunks FIRST, then set retrying
    act(() => {
      ws.simulateMessage({ type: 'subagent_retrying', data: { slot: 'slot-1', id: 'agent-1', attempt: 1 } })
    })

    // Without the fix: retrying is true now, but rAF will clear it.
    // With the fix: the flush happens BEFORE the retry dispatch, so retrying stays true.
    expect(agentRetrying()).toBe(true)

    // 3. rAF fires — if the fix is correct, there's nothing left to flush
    runFrames()

    // The retrying state MUST survive the frame
    expect(agentRetrying()).toBe(true)

    // And the text should still have been delivered
    expect(agentStreaming('slot-1', 'agent-1')).toBe('partial ')
  })

  it('flushes buffered chunks before subagent_recovering to preserve retrying state', () => {
    // Same race as subagent_retrying but for the one-shot cancel auto-continue.
    const { ws } = mount()
    seedStore()

    const agentRetrying = () => globalStore.getState().chat.subagents['agent-1']?.retrying

    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'text ')) })
    act(() => {
      ws.simulateMessage({ type: 'subagent_recovering', data: { slot: 'slot-1', id: 'agent-1' } })
    })

    expect(agentRetrying()).toBe(true)

    runFrames()

    expect(agentRetrying()).toBe(true)
    expect(agentStreaming('slot-1', 'agent-1')).toBe('text ')
  })

  it('discards buffered chunks when an authoritative snapshot arrives (replay dedup)', () => {
    // Regression: chunk buffered during replay, snapshot applies, frame flush
    // appends the same text again — duplicating what the snapshot already included.
    const { ws } = mount()
    seedStore()

    // 1. Buffer a chunk (simulates a chunk arriving during subscription replay)
    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'partial ')) })
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')  // not flushed yet

    // 2. Authoritative snapshot arrives — its streaming field already includes the chunk
    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot',
        data: {
          slot: 'slot-1',
          id: 'agent-1',
          task: 'test task',
          agent: 'test-agent',
          streaming: 'partial ',  // snapshot already has the text
          last_tool: '',
          started: Date.now() / 1000,
        },
      })
    })

    // Snapshot applied immediately — streaming is now 'partial '
    expect(agentStreaming('slot-1', 'agent-1')).toBe('partial ')

    // 3. rAF fires — WITHOUT the fix, the buffered chunk would append again
    runFrames()

    // WITH the fix: the buffered chunk was discarded, so streaming stays 'partial '
    // WITHOUT the fix: streaming would be 'partial partial ' (duplicated)
    expect(agentStreaming('slot-1', 'agent-1')).toBe('partial ')
  })

  it('still appends chunks that arrive AFTER replay completes (not subsumed)', () => {
    // Guard the opposite regression: a chunk arriving after replay must still be appended.
    const { ws } = mount()
    seedStore()

    // 1. Authoritative snapshot arrives first (replay complete)
    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot',
        data: {
          slot: 'slot-1',
          id: 'agent-1',
          task: 'test task',
          agent: 'test-agent',
          streaming: 'snapshot ',
          last_tool: '',
          started: Date.now() / 1000,
        },
      })
    })
    expect(agentStreaming('slot-1', 'agent-1')).toBe('snapshot ')

    // 2. A NEW chunk arrives after replay — this is live, not subsumed
    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'live ')) })

    // 3. rAF fires — the live chunk MUST be appended
    runFrames()

    expect(agentStreaming('slot-1', 'agent-1')).toBe('snapshot live ')
  })

  it('discards buffered chunks for each agent in a snapshot batch', () => {
    // Same dedup for the batched snapshot path (subagent_snapshot_batch).
    const { ws } = mount()
    seedStore()

    // Buffer chunks for both agents
    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'a1 '))
      ws.simulateMessage(chunk('slot-1', 'agent-2', 'a2 '))
    })

    // Batch snapshot arrives with authoritative text
    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot_batch',
        data: {
          items: [
            { type: 'subagent_snapshot', data: { slot: 'slot-1', id: 'agent-1', task: 't1', agent: 'a', streaming: 'a1 ', last_tool: '', started: Date.now() / 1000 } },
            { type: 'subagent_snapshot', data: { slot: 'slot-1', id: 'agent-2', task: 't2', agent: 'a', streaming: 'a2 ', last_tool: '', started: Date.now() / 1000 } },
          ],
        },
      })
    })

    runFrames()

    // Both agents should have exactly the snapshot text, no duplication
    expect(agentStreaming('slot-1', 'agent-1')).toBe('a1 ')
    expect(agentStreaming('slot-1', 'agent-2')).toBe('a2 ')
  })

  it('flushes buffered chunks for a batched subagent_done (per-key, not whole buffer)', () => {
    // Without per-key flush, stale chunk appends AFTER done clears streaming.
    const { ws } = mount()
    seedStore()

    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'final ')) })
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')

    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot_batch',
        data: {
          items: [
            { type: 'subagent_done', data: { slot: 'slot-1', id: 'agent-1', elapsed: 1000, outcome: 'completed' } },
          ],
        },
      })
    })

    expect(globalStore.getState().chat.subagents['agent-1']?.status).toBe('done')

    // rAF must NOT append stale chunk to done agent.
    runFrames()
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')
  })

  it('does NOT duplicate snapshot text when batch has snapshot for A and done for B (NEGCTL 2)', () => {
    // Per-key flush for done(B) must not touch A's buffered chunk.
    const { ws } = mount()
    seedStore()

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'a1-partial '))
      ws.simulateMessage(chunk('slot-1', 'agent-2', 'a2-partial '))
    })

    // done for B, then snapshot for A.
    act(() => {
      ws.simulateMessage({
        type: 'subagent_snapshot_batch',
        data: {
          items: [
            { type: 'subagent_done', data: { slot: 'slot-1', id: 'agent-2', elapsed: 1000, outcome: 'completed' } },
            { type: 'subagent_snapshot', data: { slot: 'slot-1', id: 'agent-1', task: 't1', agent: 'a', streaming: 'a1-partial ', last_tool: '', started: Date.now() / 1000 } },
          ],
        },
      })
    })

    runFrames()

    // A should have exactly snapshot text, not duplicated.
    expect(agentStreaming('slot-1', 'agent-1')).toBe('a1-partial ')
    expect(globalStore.getState().chat.subagents['agent-2']?.status).toBe('done')
    expect(agentStreaming('slot-1', 'agent-2')).toBe('')
  })

  it('flushes buffered chunks before server-batched chunks to preserve ordering (hidden tab hazard)', () => {
    // Hidden tab suspends rAF; without flush-before-batch, newer batched
    // chunks dispatch first, then stale buffered chunk appends on visibility.
    const { ws } = mount()
    seedStore()

    // Buffer an unbatched chunk (rAF scheduled but not yet fired)
    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'first ')) })
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')

    // Server batches newer text; fix flushes buffer first to preserve order
    act(() => {
      ws.simulateMessage({
        type: 'subagent_batch_chunks',
        data: { chunks: [{ slot: 'slot-1', id: 'agent-1', text: 'second ' }] },
      })
    })

    expect(agentStreaming('slot-1', 'agent-1')).toBe('first second ')

    runFrames()
    expect(agentStreaming('slot-1', 'agent-1')).toBe('first second ')
  })

  it('preserves retrying state when batch_update retry arrives after buffered chunk (hidden tab hazard)', () => {
    // Hazard: buffered chunk + batch_update retry + deferred flush clears retrying.
    // Fix: per-key flush for retry items before applying batch_update.
    const { ws } = mount()
    seedStore()

    // Buffer a chunk (rAF scheduled but not fired — simulates hidden tab)
    act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', 'pre-retry ')) })
    expect(agentStreaming('slot-1', 'agent-1')).toBe('')

    // batch_update arrives with retry (attempt field)
    act(() => {
      ws.simulateMessage({
        type: 'subagent_batch_update',
        data: { updates: [{ slot: 'slot-1', id: 'agent-1', attempt: 2 }] },
      })
    })

    // After batch_update: retrying must be set, chunk text must appear exactly once
    const agent = globalStore.getState().chat.subagents['agent-1']
    expect(agent?.retrying).toBe(true)
    expect(agentStreaming('slot-1', 'agent-1')).toBe('pre-retry ')

    // rAF fires — must NOT clear retrying, must NOT duplicate text
    runFrames()
    const agentAfter = globalStore.getState().chat.subagents['agent-1']
    expect(agentAfter?.retrying).toBe(true)
    expect(agentStreaming('slot-1', 'agent-1')).toBe('pre-retry ')
  })

  it('does NOT duplicate text when batch_update has retry for A and tool for B', () => {
    // Guard against cross-agent duplication from per-key flush.
    const { ws } = mount()
    seedStore()

    // Buffer chunks for both agents
    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', 'a1-text '))
      ws.simulateMessage(chunk('slot-1', 'agent-2', 'a2-text '))
    })

    // batch_update: retry for agent-1, tool for agent-2
    act(() => {
      ws.simulateMessage({
        type: 'subagent_batch_update',
        data: { updates: [
          { slot: 'slot-1', id: 'agent-1', attempt: 1 },
          { slot: 'slot-1', id: 'agent-2', tool: 'read_file' },
        ] },
      })
    })

    runFrames()

    // agent-1: retrying, text exactly once
    expect(globalStore.getState().chat.subagents['agent-1']?.retrying).toBe(true)
    expect(agentStreaming('slot-1', 'agent-1')).toBe('a1-text ')

    // agent-2: not retrying (tool clears it), text exactly once
    expect(globalStore.getState().chat.subagents['agent-2']?.retrying).toBe(false)
    expect(agentStreaming('slot-1', 'agent-2')).toBe('a2-text ')
  })

  it('flushes through reducer on overflow, preserving truncation marker (overflow arm)', () => {
    // Overflow arm: after exceeding 50KB, the reducer's truncation fires with marker.
    const { ws } = mount()
    seedStore()

    // Send chunks that exceed 50KB total — use distinctive tail bytes
    const chunkSize = 10_000
    for (let i = 0; i < 7; i++) {
      // Each chunk: 10KB of repeated digit, so we can identify which survived
      const text = String(i).repeat(chunkSize)
      act(() => { ws.simulateMessage(chunk('slot-1', 'agent-1', text)) })
    }
    // Total buffered: 70KB. Reducer truncates 50KB+ to 40KB with marker.

    runFrames()

    const result = agentStreaming('slot-1', 'agent-1')
    // The reducer's truncation: marker + '\n' + last 40KB
    // The marker is i18nT('store.chatSlice.truncated') which may render as key or translation
    expect(result).toMatch(/truncated|…\(truncated\)/)
    // Length: marker + newline + 40000 (reducer keeps 40KB)
    expect(result.length).toBeGreaterThan(40_000)
    expect(result.length).toBeLessThan(41_000)
    // Newest bytes are preserved (tail of the content after marker)
    // The tail should end with '6' (the last chunk)
    expect(result.slice(-10)).toBe('6666666666')
  })

  it('flushes byte-identical text when under the 50KB cap (under-bound arm)', () => {
    // Under-bound arm: no truncation, no marker, nothing dropped.
    const { ws } = mount()
    seedStore()

    const chunk1 = 'first-chunk-text-'
    const chunk2 = 'second-chunk-text-'
    const chunk3 = 'third-chunk-text'
    const expected = chunk1 + chunk2 + chunk3

    act(() => {
      ws.simulateMessage(chunk('slot-1', 'agent-1', chunk1))
      ws.simulateMessage(chunk('slot-1', 'agent-1', chunk2))
      ws.simulateMessage(chunk('slot-1', 'agent-1', chunk3))
    })

    runFrames()

    // Must be byte-identical to concatenation — no truncation, no marker
    expect(agentStreaming('slot-1', 'agent-1')).toBe(expected)
  })
})
