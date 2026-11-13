/**
 * Read aloud and auto-speak in a crewmate's chat (#29689). The Crewmates page
 * shows a crewmate's thread without moving `chat.activeSlot`, so playback that
 * followed the active chat alone dropped every audio frame for the crewmate's
 * replies. The mounted crewmate thread is the slot read aloud while it is
 * open. The frame-router setup follows UseWebSocketCoverage.voice.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages, hydrateSlotMessages } from '../store/chatSlice'
import { clearMountedMemberThreadSlot, setMountedMemberThreadSlot } from '../lib/mountedMemberThread'
import { ACTIVE, BACKGROUND, WS_INSTANCES, MockWebSocket, MockAudio } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    answerQuestion: vi.fn().mockResolvedValue({ ok: true }),
    voiceSynthesize: vi.fn().mockResolvedValue({ ok: true }),
    voiceCancel: vi.fn().mockResolvedValue({ ok: true }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

// The crewmate thread: a slot that is NOT `chat.activeSlot`.
const MATE = BACKGROUND

describe('voice playback in a crewmate thread', () => {
  let testStore: ReturnType<typeof createTestStore>
  let rafCbs: FrameRequestCallback[]
  let originalCreateObjectUrl: typeof URL.createObjectURL
  let originalRevokeObjectUrl: typeof URL.revokeObjectURL

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    MockAudio.instances.length = 0
    MockAudio.playResult = () => Promise.resolve()
    rafCbs = []
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Audio', MockAudio)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    originalCreateObjectUrl = URL.createObjectURL
    originalRevokeObjectUrl = URL.revokeObjectURL
    let blobSeq = 0
    URL.createObjectURL = vi.fn(() => `blob:voice-${++blobSeq}`)
    URL.revokeObjectURL = vi.fn()
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    clearMountedMemberThreadSlot()
    vi.unstubAllGlobals()
    URL.createObjectURL = originalCreateObjectUrl
    URL.revokeObjectURL = originalRevokeObjectUrl
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  const audioCount = () => (URL.createObjectURL as ReturnType<typeof vi.fn>).mock.calls.length

  it('plays a Read aloud request for the open crewmate thread', async () => {
    const { ws } = mount()
    act(() => { setMountedMemberThreadSlot(MATE) })
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: MATE, request_id: 'mate-voice' } }))
    })
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'mate-voice', slot: MATE, audio: btoa('mate') } })
    })
    expect(audioCount()).toBe(1)
    expect(testStore.getState().chat.voicePlaying).toBe(true)
  })

  it('ignores the crewmate slot once its thread is closed', async () => {
    const { ws } = mount()
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: MATE, request_id: 'mate-voice' } }))
    })
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'mate-voice', slot: MATE, audio: btoa('mate') } })
    })
    expect(audioCount()).toBe(0)
  })

  it('auto-speaks the crewmate reply when its turn finishes', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      setMountedMemberThreadSlot(MATE)
      globalStore.dispatch(hydrateSlotMessages({
        slot: MATE,
        messages: [{ role: 'assistant', content: 'The crewmate finished the task.', ts: '1' }],
        hasMore: false,
      }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: MATE } }) })
    expect(api.voiceSynthesize).toHaveBeenCalledWith(MATE, 'The crewmate finished the task.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('does not read an earlier reply again when the newest turn said nothing', async () => {
    // Its own slot: the singleton store keeps the earlier tests' rows.
    const SILENT = 'member-silent'
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      setMountedMemberThreadSlot(SILENT)
      globalStore.dispatch(hydrateSlotMessages({
        slot: SILENT,
        messages: [
          { role: 'assistant', content: 'An earlier answer.', ts: '1' },
          { role: 'user', content: 'And now?', ts: '2' },
          { role: 'error', content: 'Sign in again to continue.', ts: '3' },
        ],
        hasMore: false,
      }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: SILENT } }) })
    expect(api.voiceSynthesize).not.toHaveBeenCalled()
  })

  it('does not read a compaction notice the crewmate chat hides', async () => {
    const COMPACTED = 'member-compacted'
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      setMountedMemberThreadSlot(COMPACTED)
      globalStore.dispatch(hydrateSlotMessages({
        slot: COMPACTED,
        messages: [
          { role: 'assistant', content: 'An earlier answer.', ts: '1' },
          { role: 'user', content: '/compact', ts: '2' },
          { role: 'assistant', content: 'Context summary: the conversation so far.', ts: '3', meta: { kind: 'compaction' } },
        ],
        hasMore: false,
      }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: COMPACTED } }) })
    expect(api.voiceSynthesize).not.toHaveBeenCalled()
  })

  it('reads the crewmate reply that comes before a hidden notice in the same turn', async () => {
    const NOTICED = 'member-noticed'
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      setMountedMemberThreadSlot(NOTICED)
      globalStore.dispatch(hydrateSlotMessages({
        slot: NOTICED,
        messages: [
          { role: 'user', content: 'Summarize the run.', ts: '1' },
          { role: 'assistant', content: 'The run passed.', ts: '2' },
          { role: 'assistant', content: 'Context summary: the conversation so far.', ts: '3', meta: { kind: 'compaction' } },
        ],
        hasMore: false,
      }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: NOTICED } }) })
    expect(api.voiceSynthesize).toHaveBeenCalledWith(NOTICED, 'The run passed.', expect.objectContaining({ request_id: expect.any(String) }))
  })
})
