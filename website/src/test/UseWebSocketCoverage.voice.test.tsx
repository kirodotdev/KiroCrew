/**
 * Behavioural coverage for the multiplexed dashboard socket: the frame router's
 * voice arms -- the playback queue, the preparing state, interruption, and
 * auto-speak. The frame-router setup repeats
 * UseWebSocketCoverage.test.tsx's (keep the copies identical); the fakes are in
 * UseWebSocketCoverageTestUtils.tsx.
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
import chatReducer, { setActiveSlot, clearMessages, sseChatMessage } from '../store/chatSlice'
import { recentErrors } from '../utils/errorReport'
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

describe('useWebSocket frame router', () => {
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
    // The production tree dispatches and reads through the same singleton
    // store. This harness intentionally dispatches through a Provider test
    // store, so mirror only production-marked coordinator rows into the
    // singleton read side; unmarked runner rows remain explicit per test.
    const mirroredCoordinatorRows = new Set<string>()
    testStore.subscribe(() => {
      const state = testStore.getState().chat
      const bySlot: Array<[string | null, typeof state.messages]> = [
        [state.activeSlot, state.messages],
        ...Object.entries(state.slotMessages),
      ]
      for (const [slot, messages] of bySlot) {
        if (!slot) continue
        for (const message of messages ?? []) {
          const id = message.meta?.approval_id
          if (message.role !== 'permission'
            || message.meta?.registry !== 'coordinator'
            || typeof id !== 'string') continue
          const key = `${slot}\u0000${id}\u0000${message.ts ?? ''}`
          if (mirroredCoordinatorRows.has(key)) continue
          mirroredCoordinatorRows.add(key)
          globalStore.dispatch(sseChatMessage({
            slot,
            role: message.role,
            content: message.content,
            ts: message.ts,
            cls: message.cls,
            meta: message.meta,
          }))
        }
      }
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    originalCreateObjectUrl = URL.createObjectURL
    originalRevokeObjectUrl = URL.revokeObjectURL
    let blobSeq = 0
    URL.createObjectURL = vi.fn(() => `blob:voice-${++blobSeq}`)
    URL.revokeObjectURL = vi.fn()
    // Read paths resolve against the singleton store, not the Provider store.
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
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
    act(() => {
      ws.simulateOpen()
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot: ACTIVE, request_id: 'test-voice' } }))
    })
    return { ...hook, ws }
  }

  const chat = () => testStore.getState().chat

  it('queues, plays and drains voice chunks for the viewed slot', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    const audio = () => (URL.createObjectURL as ReturnType<typeof vi.fn>).mock.calls.length

    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('first') } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('second') } })
    })
    expect(audio()).toBe(2)
    expect(chat().voicePlaying).toBe(true)
    // Only the head plays; the tail waits for `onended`.
    expect(MockAudio.instances).toHaveLength(1)

    act(() => { MockAudio.instances[0].onended?.() })
    expect(MockAudio.instances).toHaveLength(2)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-1')

    // Draining the last chunk stops the playing indicator.
    act(() => { MockAudio.instances[1].onended?.() })
    expect(chat().voicePlaying).toBe(false)
  })

  it('marks the viewed slot as preparing until its first voice chunk arrives', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    expect(chat().voicePreparing).toBe(true)
    expect(chat().voicePlaying).toBe(false)

    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('first') } })
    })
    expect(chat().voicePreparing).toBe(false)
    expect(chat().voicePlaying).toBe(true)
  })

  it('ends preparing when synthesis errors, fails over HTTP or is stopped, and ignores a background slot', () => {
    const { ws } = mount()
    const start = (slot: string, request_id: string) => {
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot, request_id } }))
    }

    act(() => { ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'test-voice', code: 'voice_unavailable' } }) })
    expect(chat().voicePreparing).toBe(false)

    act(() => { start(ACTIVE, 'http') })
    expect(chat().voicePreparing).toBe(true)
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-synthesis-failed', { detail: { slot: ACTIVE, request_id: 'http', code: 'voice_unavailable' } }))
    })
    expect(chat().voicePreparing).toBe(false)

    act(() => { start(ACTIVE, 'stopped') })
    expect(chat().voicePreparing).toBe(true)
    act(() => { window.dispatchEvent(new Event('voice-stop')) })
    expect(chat().voicePreparing).toBe(false)

    act(() => { start(BACKGROUND, 'other') })
    expect(chat().voicePreparing).toBe(false)
  })

  it('ends preparing when the user switches chats, so a late chunk for the old chat cannot strand it', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    expect(chat().voicePreparing).toBe(true)

    act(() => { globalStore.dispatch(setActiveSlot(BACKGROUND)) })
    expect(chat().voicePreparing).toBe(false)

    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('late') } })
      ws.simulateMessage({ type: 'voice_complete', data: { request_id: 'test-voice', slot: ACTIVE } })
    })
    expect(chat().voicePreparing).toBe(false)
    expect(chat().voicePlaying).toBe(false)
  })

  it('uses the WAV MIME type supplied with a local voice chunk', async () => {
    const blobs: Blob[] = []
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob)
      return 'blob:voice-wav'
    })
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()

    await act(async () => {
      ws.simulateMessage({
        type: 'voice_chunk',
        data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('wav'), audioMime: 'audio/wav' },
      })
    })

    expect(blobs).toHaveLength(1)
    expect(blobs[0].type).toBe('audio/wav')
  })

  it.each(['media-error', 'autoplay-blocked'])('cancels the entire voice queue after %s', async failure => {
    let rejectPlayback!: (reason: Error) => void
    MockAudio.playResult = () => new Promise((_, reject) => { rejectPlayback = reject })
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    const errors = vi.fn()
    window.addEventListener('voice-error', errors)
    try {
      await act(async () => {
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('a') } })
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('b') } })
      })
      await act(async () => {
        if (failure === 'media-error') MockAudio.instances[0].onerror?.()
        else rejectPlayback(new DOMException('autoplay blocked', 'NotAllowedError'))
      })
      await act(async () => {
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('late') } })
      })
      expect(errors).toHaveBeenCalledOnce()
      expect((errors.mock.calls[0][0] as CustomEvent).detail.code).toBe(
        failure === 'media-error' ? 'voice_playback_failed' : 'voice_playback_blocked',
      )
      expect(MockAudio.instances).toHaveLength(1)
      expect(MockAudio.instances[0].pause).toHaveBeenCalledOnce()
      expect(api.voiceCancel).toHaveBeenCalledWith(ACTIVE, 'test-voice')
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-1')
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-2')
      expect(chat().voicePlaying).toBe(false)
    } finally {
      window.removeEventListener('voice-error', errors)
    }
  })

  it('drops voice audio for a background slot and a malformed payload', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: BACKGROUND, audio: btoa('x') } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: 'not-base64-@@@' } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE } })
    })
    expect(MockAudio.instances).toHaveLength(0)
    expect(chat().voicePlaying).toBe(false)
  })

  it('stores the stitched replay audio from voice_complete', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'voice_complete', data: { slot: ACTIVE, request_id: 'test-voice', audio: 'BASE64MP3' } }) })
    expect(chat().voiceAudio).toBe('BASE64MP3')

    act(() => { ws.simulateMessage({ type: 'voice_complete', data: {} }) })
    expect(chat().voiceAudio).toBe('BASE64MP3')
  })

  it('journals an asynchronous synthesis failure with no ChatPage listener mounted', () => {
    const { ws } = mount()
    const before = recentErrors().map(report => report.id)
    act(() => {
      ws.simulateMessage({ type: 'voice_error', data: {
        slot: ACTIVE, request_id: 'test-voice', code: 'voice_synthesis_failed',
      } })
      // A repeated terminal frame belongs to the already-consumed request.
      ws.simulateMessage({ type: 'voice_error', data: {
        slot: ACTIVE, request_id: 'test-voice', code: 'voice_synthesis_failed',
      } })
    })
    const added = recentErrors().filter(report => !before.includes(report.id))
    expect(added).toHaveLength(1)
    expect(added[0]).toMatchObject({
      source: 'system', code: 'voice_synthesis_failed', endpoint: '/api/voice/synthesize',
    })
  })

  it('interrupts playback and empties the queue on a voice-stop event', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('a') } })
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('b') } })
    })
    expect(chat().voicePlaying).toBe(true)

    act(() => { window.dispatchEvent(new Event('voice-stop')) })
    expect(MockAudio.instances[0].pause).toHaveBeenCalled()
    expect(chat().voicePlaying).toBe(false)
    // The un-played tail url was released, not leaked.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-2')

    // Muted afterwards: a further chunk is ignored.
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('c') } })
    })
    expect(chat().voicePlaying).toBe(false)
  })

  it('interrupts playback when the user speaks over the agent', async () => {
    vi.stubGlobal('Audio', MockAudio)
    const { ws } = mount()
    await act(async () => {
      ws.simulateMessage({ type: 'voice_chunk', data: { request_id: 'test-voice', slot: ACTIVE, audio: btoa('a') } })
    })
    expect(chat().voicePlaying).toBe(true)

    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'user', content: 'stop', ts: '9' } })
    })
    expect(chat().voicePlaying).toBe(false)
  })

  it('speaks completed sentences once per flush when auto-speak is on', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    // The auto-speak reader looks at the SINGLETON store's streaming message.
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'chunk', content: 'This sentence is long enough. ', batched: true,
      }))
    })

    await act(async () => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'This sentence is long enough. ', seq: 1 } })
    })
    await act(async () => { rafCbs[0](0) })

    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'This sentence is long enough.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('does not re-speak a sentence it already sent, nor a fragment', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'chunk', content: 'Short. and then an unterminated tail', batched: true,
      }))
    })
    await act(async () => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content: 'x', seq: 1 } })
    })
    await act(async () => { rafCbs[0](0) })
    // A short sentence is still speakable; the unfinished tail waits.
    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'Short.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('speaks the unspoken tail when the turn finishes', async () => {
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ autoSpeak: true })
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })
    act(() => {
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'assistant', content: 'A complete final answer.', ts: '1',
      }))
    })

    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'A complete final answer.', expect.objectContaining({ request_id: expect.any(String) }))
  })

  it('re-reads the auto-speak preference when the finished turn had it off', async () => {
    const { ws } = mount()
    ;(api.voiceConfig as ReturnType<typeof vi.fn>).mockClear()
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceConfig).toHaveBeenCalled()
  })

  it('follows the auto-speak preference when the settings pane changes it', async () => {
    const { ws } = mount()
    await act(async () => { await Promise.resolve() })  // settle the on-open config read
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { autoSpeak: true } }))
      globalStore.dispatch(sseChatMessage({
        slot: ACTIVE, role: 'assistant', content: 'Spoken because the pane turned it on.', ts: '1',
      }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceSynthesize).toHaveBeenCalledWith(ACTIVE, 'Spoken because the pane turned it on.', expect.objectContaining({ request_id: expect.any(String) }))

    // ...and switching it back off silences the next turn.
    ;(api.voiceSynthesize as ReturnType<typeof vi.fn>).mockClear()
    act(() => {
      window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { autoSpeak: false } }))
    })
    await act(async () => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })
    expect(api.voiceSynthesize).not.toHaveBeenCalled()
  })
})
