/**
 * Restart on the Read aloud playback bar: the `voice-restart` event speaks the
 * current playback context again from its first sentence. These tests drive the
 * real useWebSocket voice owner, with `voiceSynthesize` announcing each request
 * on `voice-synthesis-start` the way the real client does.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWebSocket } from '../hooks/useWebSocket'
import { store } from '../store'
import { setActiveSlot, clearSlotState } from '../store/chatSlice'
import { speakReply } from '../pages/chat/speakReply'
import { api } from '../api/client'
import { ACTIVE, WS_INSTANCES, MockWebSocket, MockAudio } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: true }),
    voiceSynthesize: vi.fn(),
    voiceCancel: vi.fn().mockResolvedValue({}),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const synthesize = api.voiceSynthesize as unknown as ReturnType<typeof vi.fn>
const cancel = api.voiceCancel as unknown as ReturnType<typeof vi.fn>

const FIRST = 'This is the first spoken sentence. '
const SECOND = 'Here is the second spoken sentence. '
const THIRD = 'And the third sentence arrives later. '

/** Texts handed to the synthesis API, in call order. */
const synthesizedTexts = () => synthesize.mock.calls.map(call => call[1] as string)
/** The request id of the nth synthesis call. */
const requestIdOf = (call: number) => (synthesize.mock.calls[call][2] as { request_id: string }).request_id

describe('Read aloud restart', () => {
  let rafCallbacks: FrameRequestCallback[]
  let nextRequest: number

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    MockAudio.instances.length = 0
    rafCallbacks = []
    nextRequest = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('Audio', MockAudio)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCallbacks.push(cb); return rafCallbacks.length })
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:voice-${++nextRequest}`)
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    synthesize.mockImplementation((slot: string, text: string, opts: { request_id?: string; clientFallback?: boolean } = {}) => {
      const request_id = opts.request_id ?? `manual-${++nextRequest}`
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', {
        detail: { slot, request_id, clientFallback: !!opts.clientFallback, text },
      }))
      return Promise.resolve({ ok: true })
    })
    store.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    store.dispatch(clearSlotState())
    store.dispatch(setActiveSlot(null))
  })

  async function mount() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const hook = renderHook(() => useWebSocket(), {
      wrapper: ({ children }: { children: React.ReactNode }) =>
        createElement(Provider, { store }, createElement(QueryClientProvider, { client: queryClient }, children)),
    })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    await act(async () => {})
    return { hook, ws }
  }

  /** Stream `content` into the on-screen reply and run the flush that speaks completed sentences. */
  async function stream(ws: MockWebSocket, content: string, seq: number) {
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: ACTIVE, content, seq } }) })
    await act(async () => { rafCallbacks.splice(0).forEach(callback => callback(0)) })
  }

  const restart = async () => {
    await act(async () => { window.dispatchEvent(new Event('voice-restart')) })
  }
  const stopReading = () => act(() => { window.dispatchEvent(new Event('voice-stop')) })
  const chunkFor = (ws: MockWebSocket, request_id: string, audio: string) => act(() => {
    ws.simulateMessage({ type: 'voice_chunk', data: { request_id, slot: ACTIVE, audio: btoa(audio) } })
  })
  const chat = () => store.getState().chat

  describe('auto-speak', () => {
    it('speaks the reply again from its first sentence, each saved request on its own', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      await stream(ws, SECOND, 2)
      expect(synthesizedTexts()).toEqual([FIRST.trim(), SECOND.trim()])
      expect(chat().voicePreparing).toBe(true)

      await restart()

      expect(synthesizedTexts()).toEqual([FIRST.trim(), SECOND.trim(), FIRST.trim(), SECOND.trim()])
      expect(chat().voicePreparing).toBe(true)
      expect(cancel).toHaveBeenCalledWith(ACTIVE, requestIdOf(0))
      hook.unmount()
    })

    it('restarts while the audio is still being prepared', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      expect(chat().voicePreparing).toBe(true)
      expect(chat().voicePlaying).toBe(false)

      await restart()

      expect(synthesizedTexts()).toEqual([FIRST.trim(), FIRST.trim()])
      expect(chat().voicePreparing).toBe(true)
      expect(chat().voicePlaying).toBe(false)
      hook.unmount()
    })

    it('keeps the reply that is still streaming: later sentences follow the replayed ones, none repeated', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      await restart()
      await stream(ws, SECOND, 2)

      expect(synthesizedTexts()).toEqual([FIRST.trim(), FIRST.trim(), SECOND.trim()])

      await restart()
      expect(synthesizedTexts().slice(3)).toEqual([FIRST.trim(), SECOND.trim()])

      await stream(ws, THIRD, 3)
      expect(synthesizedTexts().slice(5)).toEqual([THIRD.trim()])
      hook.unmount()
    })

    it('replays the text a sentence was merged into while its predecessor synthesized', async () => {
      const { hook, ws } = await mount()
      synthesize.mockImplementationOnce(() => new Promise(() => {}))
      await stream(ws, FIRST, 1)
      await stream(ws, SECOND, 2)
      await stream(ws, THIRD, 3)
      expect(synthesizedTexts()).toEqual([FIRST.trim()])

      await restart()

      expect(synthesizedTexts()).toEqual([FIRST.trim(), FIRST.trim(), `${SECOND.trim()}\n${THIRD.trim()}`])
      hook.unmount()
    })

    it('drops audio of the previous run and plays the new run', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      const staleRequest = requestIdOf(0)
      await restart()
      const freshRequest = requestIdOf(1)

      await act(async () => {
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: staleRequest, slot: ACTIVE, audio: btoa('stale') } })
        ws.simulateMessage({ type: 'voice_complete', data: { request_id: staleRequest, slot: ACTIVE } })
      })
      expect(MockAudio.instances).toHaveLength(0)
      expect(chat().voicePlaying).toBe(false)
      expect(chat().voicePreparing).toBe(true)

      await act(async () => { chunkFor(ws, freshRequest, 'fresh') })
      expect(MockAudio.instances).toHaveLength(1)
      expect(chat().voicePlaying).toBe(true)
      expect(chat().voicePreparing).toBe(false)
      hook.unmount()
    })

    it('silences audio that is playing and releases its queued chunks', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      await act(async () => {
        chunkFor(ws, requestIdOf(0), 'a')
        chunkFor(ws, requestIdOf(0), 'b')
      })
      expect(chat().voicePlaying).toBe(true)
      const playing = MockAudio.instances[0]

      await restart()

      expect(playing.pause).toHaveBeenCalled()
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:voice-2')
      expect(chat().voicePlaying).toBe(false)
      expect(chat().voicePreparing).toBe(true)
      hook.unmount()
    })

    it('does not synthesize a request that was queued behind the one running at restart', async () => {
      const { hook, ws } = await mount()
      let finishFirst: () => void = () => {}
      synthesize.mockImplementationOnce((slot: string, text: string, opts: { request_id: string }) => {
        window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot, request_id: opts.request_id, text } }))
        return new Promise<void>(resolve => { finishFirst = resolve })
      })
      await stream(ws, FIRST, 1)
      await stream(ws, SECOND, 2)
      expect(synthesizedTexts()).toEqual([FIRST.trim()])

      await restart()
      await act(async () => { finishFirst() })

      expect(synthesizedTexts()).toEqual([FIRST.trim(), FIRST.trim(), SECOND.trim()])
      hook.unmount()
    })
  })

  describe('while paused', () => {
    const pauseReading = () => act(() => { window.dispatchEvent(new Event('voice-pause')) })

    it('ends the pause and plays the first sentence again when the audio was paused mid-reading', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      await act(async () => { chunkFor(ws, requestIdOf(0), 'first') })
      const paused = MockAudio.instances[0]
      pauseReading()
      expect(chat().voicePaused).toBe(true)
      paused.pause.mockClear()

      await restart()

      expect(chat().voicePaused).toBe(false)
      expect(paused.pause).toHaveBeenCalled()
      expect(synthesizedTexts()).toEqual([FIRST.trim(), FIRST.trim()])
      await act(async () => { chunkFor(ws, requestIdOf(1), 'again') })
      expect(MockAudio.instances).toHaveLength(2)
      expect(MockAudio.instances[1].play).toHaveBeenCalledOnce()
      expect(chat().voicePlaying).toBe(true)
      hook.unmount()
    })

    it('ends the pause when the audio was paused while still being prepared', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      pauseReading()
      expect(chat().voicePaused).toBe(true)

      await restart()

      expect(chat().voicePaused).toBe(false)
      expect(chat().voicePreparing).toBe(true)
      await act(async () => { chunkFor(ws, requestIdOf(1), 'again') })
      expect(MockAudio.instances).toHaveLength(1)
      expect(MockAudio.instances[0].play).toHaveBeenCalledOnce()
      hook.unmount()
    })

    it('releases the audio clock a pause froze, so the replay is audible', async () => {
      const contexts: { state: string; resume: ReturnType<typeof vi.fn> }[] = []
      class FakeContext {
        state = 'running'
        currentTime = 0
        destination = {}
        decodeAudioData = vi.fn().mockResolvedValue({ duration: 0.2 })
        suspend = vi.fn(() => { this.state = 'suspended'; return Promise.resolve() })
        resume = vi.fn(() => { this.state = 'running'; return Promise.resolve() })
        close = vi.fn().mockResolvedValue(undefined)
        createBufferSource() {
          return { start: vi.fn(), stop: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), onended: null }
        }
        constructor() { contexts.push(this) }
      }
      vi.stubGlobal('AudioContext', FakeContext)
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      await act(async () => {
        ws.simulateMessage({ type: 'voice_chunk', data: { request_id: requestIdOf(0), slot: ACTIVE, audio: btoa('wav'), audioMime: 'audio/wav' } })
      })
      await vi.waitFor(() => expect(chat().voicePlaying).toBe(true))
      pauseReading()
      expect(contexts[0].state).toBe('suspended')

      await restart()

      expect(chat().voicePaused).toBe(false)
      expect(contexts[0].state).toBe('running')
      hook.unmount()
    })

    it('cancels paused client speech so the replay is not held silent', async () => {
      const cancelSpeech = vi.fn()
      const pauseSpeech = vi.fn()
      const speak = vi.fn()
      const resumeSpeech = vi.fn()
      class FakeUtterance { constructor(public text: string) {} onend = null; onerror = null }
      vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
      vi.stubGlobal('speechSynthesis', { speak, cancel: cancelSpeech, pause: pauseSpeech, resume: resumeSpeech, paused: true })
      const { hook, ws } = await mount()
      act(() => { speakReply(store.dispatch, ACTIVE, 'The whole reply.') })
      act(() => {
        ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'manual-1', code: 'voice_unavailable' } })
      })
      expect(speak).toHaveBeenCalledOnce()
      pauseReading()
      expect(chat().voicePaused).toBe(true)
      expect(pauseSpeech).toHaveBeenCalled()

      await restart()

      expect(cancelSpeech).toHaveBeenCalled()
      expect(resumeSpeech).toHaveBeenCalled()
      expect(chat().voicePaused).toBe(false)
      expect(chat().voicePreparing).toBe(true)
      hook.unmount()
    })
  })

  describe('manual Read aloud', () => {
    const REPLY = 'The whole reply, read aloud from the menu.'

    it('replays the reply with the client fallback still allowed', async () => {
      const { hook } = await mount()
      act(() => { speakReply(store.dispatch, ACTIVE, REPLY) })
      expect(synthesize).toHaveBeenLastCalledWith(ACTIVE, REPLY, { clientFallback: true })

      await restart()

      expect(synthesize).toHaveBeenCalledTimes(2)
      expect(synthesize).toHaveBeenLastCalledWith(ACTIVE, REPLY, { request_id: expect.any(String), clientFallback: true })
      expect(chat().voicePreparing).toBe(true)
      hook.unmount()
    })

    it('speaks on the client when the gateway cannot synthesize the restarted reply', async () => {
      const spoken: string[] = []
      class FakeUtterance { constructor(public text: string) {} onend = null; onerror = null }
      vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
      vi.stubGlobal('speechSynthesis', { speak: (u: FakeUtterance) => spoken.push(u.text), cancel: vi.fn() })
      const { hook, ws } = await mount()
      act(() => { speakReply(store.dispatch, ACTIVE, REPLY) })

      await restart()
      const restartedRequest = requestIdOf(1)
      act(() => {
        ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: restartedRequest, code: 'voice_unavailable' } })
      })

      expect(spoken).toEqual([REPLY])
      hook.unmount()
    })

    it('replays only the newest reply after a second Read aloud replaced the first', async () => {
      const { hook } = await mount()
      act(() => { speakReply(store.dispatch, ACTIVE, 'The first reply.') })
      act(() => { speakReply(store.dispatch, ACTIVE, 'The second reply.') })

      await restart()

      expect(synthesize.mock.calls.slice(2).map(call => call[1])).toEqual(['The second reply.'])
      hook.unmount()
    })
  })

  describe('after a speed change', () => {
    const changeSpeed = (rate: string) => act(() => {
      window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { rate } }))
    })

    it('keeps the reading going when the speed changes', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      await act(async () => { chunkFor(ws, requestIdOf(0), 'a') })
      expect(chat().voicePlaying).toBe(true)

      changeSpeed('120%')

      expect(chat().voicePlaying).toBe(true)
      expect(cancel).not.toHaveBeenCalled()
      hook.unmount()
    })

    it('replays through the gateway synthesis API, which applies the stored rate to each request', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      changeSpeed('150%')

      await restart()

      expect(synthesizedTexts()).toEqual([FIRST.trim(), FIRST.trim()])
      expect(chat().voicePreparing).toBe(true)
      hook.unmount()
    })

    it('speaks the restarted reply on the client at the new speed', async () => {
      const utterances: Array<{ text: string; rate: number }> = []
      class FakeUtterance { rate = 1; onend = null; onerror = null; constructor(public text: string) {} }
      vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance)
      vi.stubGlobal('speechSynthesis', { speak: (u: FakeUtterance) => utterances.push({ text: u.text, rate: u.rate }), cancel: vi.fn() })
      const { hook, ws } = await mount()
      act(() => { speakReply(store.dispatch, ACTIVE, 'The reply.') })
      changeSpeed('150%')

      await restart()
      act(() => {
        ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: requestIdOf(1), code: 'voice_unavailable' } })
      })

      expect(utterances).toEqual([{ text: 'The reply.', rate: 1.5 }])
      hook.unmount()
    })
  })

  describe('when there is nothing to restart', () => {
    it('does nothing before anything was spoken', async () => {
      const { hook } = await mount()
      await restart()
      expect(synthesize).not.toHaveBeenCalled()
      expect(cancel).not.toHaveBeenCalled()
      hook.unmount()
    })

    it('does nothing after Stop, and a stopped reply stays stopped', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      stopReading()
      await restart()
      expect(synthesizedTexts()).toEqual([FIRST.trim()])
      expect(chat().voicePreparing).toBe(false)
      hook.unmount()
    })

    it('forgets the previous reply when a new turn starts', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      act(() => { ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'user', content: 'next question', ts: '9' } }) })
      await restart()
      expect(synthesizedTexts()).toEqual([FIRST.trim()])
      hook.unmount()
    })

    it('forgets the reply when the active slot changes', async () => {
      const { hook, ws } = await mount()
      await stream(ws, FIRST, 1)
      act(() => { store.dispatch(setActiveSlot('slot-other')) })
      act(() => { store.dispatch(setActiveSlot(ACTIVE)) })
      await restart()
      expect(synthesizedTexts()).toEqual([FIRST.trim()])
      hook.unmount()
    })
  })
})
