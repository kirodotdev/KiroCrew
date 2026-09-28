/**
 * The chunk buffer drains once per animation frame, and a hidden window never
 * runs that frame. Past CHUNK_BUF_FLUSH_CHARS the buffer must drain
 * synchronously so a backgrounded renderer streaming a long turn holds at
 * most one threshold's worth of text outside the store — the same guard the
 * subagent buffer applies. Frames are hand-driven here and never run, which
 * is exactly the hidden-window condition.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import { setActiveSlot, clearSlotState, deleteSlot } from '../store/chatSlice'

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

/** The SINGLETON store on purpose: useWebSocket dispatches via useAppDispatch()
 *  but reads state off the imported singleton. */
function seedStore() {
  globalStore.dispatch(clearSlotState())
  globalStore.dispatch(setActiveSlot('slot-1'))
}

const streamingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'streaming')?.content ?? ''

const thinkingText = () =>
  globalStore.getState().chat.messages.find(m => m.role === 'thinking' && m.content)?.content ?? ''

const chunk = (slot: string, content: string, seq: number) => ({
  type: 'chat_chunk',
  data: { slot, content, seq },
})

const thinking = (slot: string, content: string) => ({
  type: 'chat_thinking',
  data: { slot, content },
})

// Mirrors CHUNK_BUF_FLUSH_CHARS in useWebSocket.ts; a pinned literal so a
// silent change to the threshold fails here rather than shifting the test.
const THRESHOLD = 50_000
const PIECE = 'p'.repeat(10_000)

describe('useWebSocket chunk buffer overflow flush (hidden window)', () => {
  let queryClient: QueryClient
  let rafQueue: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafQueue = []
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Frames are queued and NEVER run: the hidden-window condition.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQueue.push(cb); return rafQueue.length })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { rafQueue[id - 1] = () => {} })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    globalStore.dispatch(clearSlotState())
    globalStore.dispatch(setActiveSlot(null))
  })

  function mount() {
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, {
        store: globalStore,
        children: createElement(QueryClientProvider, { client: queryClient }, children),
      })
    }
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { hook, ws }
  }

  it('content below the threshold stays buffered until a frame runs', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 1; i <= 5; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))   // exactly THRESHOLD
    })
    expect(streamingText()).toBe('')
  })

  it('content past the threshold lands in the store with no frame', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 1; i <= 6; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))   // THRESHOLD + one piece
    })
    expect(streamingText().length).toBe(THRESHOLD + PIECE.length)
  })

  it('thinking past the threshold lands in the store with no frame', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 0; i < 6; i++) ws.simulateMessage(thinking('slot-1', PIECE))
    })
    expect(thinkingText().length).toBe(THRESHOLD + PIECE.length)
  })

  it('the counter restarts after an overflow flush, so the next burst buffers again', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 1; i <= 6; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))
    })
    const landed = streamingText().length
    expect(landed).toBe(THRESHOLD + PIECE.length)
    // One more piece: below the threshold again, so it waits for a frame.
    act(() => { ws.simulateMessage(chunk('slot-1', PIECE, 7)) })
    expect(streamingText().length).toBe(landed)
  })

  it('bounds mixed buffers and tool copies across repeated active/background turns', () => {
    const { ws, hook } = mount()
    seedStore()
    const slots = ['slot-1', 'overflow-background']
    const messages = (slot: string) => {
      const chat = globalStore.getState().chat
      return slot === chat.activeSlot ? chat.messages : (chat.slotMessages[slot] ?? [])
    }
    const toolLog = (slot: string) => {
      const chat = globalStore.getState().chat
      return slot === chat.activeSlot ? chat.toolLog : (chat.slotActivity[slot]?.toolLog ?? [])
    }
    const send = (type: string, data: object) => ws.simulateMessage({ type, data })
    const drainFrames = () => {
      const frames = rafQueue.splice(0)
      act(() => { frames.forEach(frame => frame(0)) })
    }
    const answers = new Map(slots.map(slot => [slot, [] as string[]]))
    try {
      for (let cycle = 0; cycle < 3; cycle++) {
        const payloads = slots.map(slot => ({
          slot,
          thought: `thought-${cycle}-${slot}:` + '想'.repeat(24_950),
          answer: `answer-${cycle}-${slot}:` + '文'.repeat(25_050),
          tail: `:tail-${cycle}-${slot}`,
          output: `head-${cycle}-${slot}:` + '首'.repeat(60_000)
            + 'discard-middle'.repeat(10_000) + '尾'.repeat(60_000) + `:end-${cycle}`,
        }))
        act(() => {
          for (const { slot } of payloads) {
            send('chat_message', { slot, role: 'user', content: `turn ${cycle}` })
            send('chat_message', {
              slot, role: 'tool', content: `read-${cycle}`,
              meta: { tool_call_id: `${slot}-${cycle}` },
            })
          }
        })
        for (const { slot, thought, answer } of payloads) {
          act(() => {
            ws.simulateMessage(thinking(slot, thought))
            ws.simulateMessage(chunk(slot, answer.slice(0, THRESHOLD - thought.length), cycle * 3 + 1))
          })
          expect(messages(slot).filter(m => m.role === 'streaming')).toEqual([])
          act(() => {
            const tip = chunk(slot, answer.slice(THRESHOLD - thought.length), cycle * 3 + 2)
            ws.simulateMessage(tip)
            ws.simulateMessage(tip) // At-least-once delivery must not grow the buffer twice.
          })
          // No frame ran: all sent text landed as soon as the shared budget overflowed.
          expect(messages(slot).filter(m => m.role === 'streaming').map(m => m.content)).toEqual([answer])
          // Background reasoning contributes to the buffer budget, but only
          // the active pane persists reasoning rows (sseThinkingChunk contract).
          if (slot === 'slot-1') {
            expect(messages(slot).filter(m => m.role === 'thinking' && m.content).at(-1)?.content).toBe(thought)
          }
          expect(toolLog(slot)).toEqual([])
        }
        act(() => {
          for (const { slot, tail, output } of payloads) {
            const tool_call_id = `${slot}-${cycle}`
            send('tool_call', { slot, tool: 'read', kind: 'read', purpose: 'test', input_preview: '', tool_call_id })
            send('tool_result', { slot, output, tool_call_id })
            ws.simulateMessage(chunk(slot, tail, cycle * 3 + 3))
          }
        })
        for (const { slot, answer } of payloads) {
          // The overflow counter restarted; the small tail still waits for completion.
          expect(messages(slot).findLast(m => m.role === 'streaming')?.content).toBe(answer)
          expect(toolLog(slot)).toHaveLength(1)
          const output = toolLog(slot)[0].output ?? ''
          expect(output.length).toBeLessThanOrEqual(64_000)
          expect(output).toContain(`head-${cycle}-${slot}:`)
          expect(output).toContain(`:end-${cycle}`)
          expect(output).not.toContain('discard-middle')
          const toolMessages = messages(slot).filter(m => m.role === 'tool')
          expect(toolMessages).toHaveLength(cycle + 1)
          expect(toolMessages.at(-1)?.meta?.tool_call_id).toBe(`${slot}-${cycle}`)
          // Ordinary results belong only in the bounded tool log, never as
          // another raw-output copy in the intentionally retained transcript.
          expect(toolMessages.every(m => m.meta?.output === undefined)).toBe(true)
        }
        act(() => {
          for (const { slot } of payloads) send('chat_done', { slot })
        })
        // Run old scheduled callbacks after completion, then start another turn.
        drainFrames()
        for (const { slot, answer, tail } of payloads) {
          answers.get(slot)!.push(answer + tail)
          expect(messages(slot).filter(m => m.role === 'streaming')).toEqual([])
          expect(messages(slot).filter(m => m.role === 'assistant').map(m => m.content)).toEqual(answers.get(slot))
          expect(toolLog(slot)).toHaveLength(1)
        }
      }
      act(() => {
        for (const slot of slots) {
          ws.simulateMessage(thinking(slot, 'discarded thought'))
          ws.simulateMessage(chunk(slot, 'discarded answer', 10))
          send('slot_clear', { slot })
        }
      })
      drainFrames()
      act(() => {
        for (const slot of slots) ws.simulateMessage(chunk(slot, `fresh-${slot}`, 11))
      })
      drainFrames()
      for (const slot of slots) {
        expect(messages(slot).map(m => m.content)).toEqual([`fresh-${slot}`])
        // Clearing the transcript preserves the last turn's bounded tool log.
        expect(toolLog(slot)).toHaveLength(1)
        expect(toolLog(slot)[0].output?.length).toBeLessThanOrEqual(64_000)
      }
    } finally {
      act(() => { slots.forEach(slot => send('slot_clear', { slot })) })
      hook.unmount()
      // Reducer-only teardown: no API call and no background slot left behind.
      slots.forEach(slot => globalStore.dispatch(deleteSlot.fulfilled(slot, 'cleanup', slot)))
      queryClient.clear()
    }
  })

  it('content and thinking share one budget per slot', () => {
    const { ws } = mount()
    seedStore()
    act(() => {
      for (let i = 0; i < 3; i++) ws.simulateMessage(thinking('slot-1', PIECE))
      for (let i = 1; i <= 2; i++) ws.simulateMessage(chunk('slot-1', PIECE, i))   // total = THRESHOLD
    })
    expect(streamingText()).toBe('')
    expect(thinkingText()).toBe('')
    act(() => { ws.simulateMessage(chunk('slot-1', PIECE, 3)) })                     // tips over
    expect(thinkingText().length).toBe(3 * PIECE.length)
    expect(streamingText().length).toBe(3 * PIECE.length)
  })
})
