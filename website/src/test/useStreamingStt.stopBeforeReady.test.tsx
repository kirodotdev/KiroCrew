/**
 * A stop that arrives BEFORE the server's `ready` must not out-run the audio.
 *
 * Capture (and therefore `recording`) begins as soon as the worklet connects,
 * but PCM cannot be SENT until the backend answers `{"type":"ready"}` — which
 * takes ~2-3s while Transcribe spins up. Everything captured in that window
 * sits in a local buffer.
 *
 * A stop frame sent inside that window ends the Transcribe stream while the
 * speech is still local, so the utterance is transcribed as silence. That is
 * the NORMAL case for a short push-to-talk tap (press, say "yes", release), not
 * an edge case, so these tests assert the ORDER of what reaches the wire rather
 * than merely that stop was called.
 *
 * The socket/worklet harness below is also what the second block uses, for the
 * other way a stop loses the dictation: a backend `error` frame that has to reach
 * the user before the close.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

vi.mock('../hooks/mic', () => ({
  acquireMicStream: () => Promise.resolve(makeStream()),
  humanizeMicError: (e: unknown) => String(e),
  createLevelMeter: () => () => {},
  setPreferredMicId: () => {},
  activeDeviceId: () => 'dev-1',
}))

const trackStops: ReturnType<typeof vi.fn>[] = []

function makeStream() {
  const track = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic', getSettings: () => ({ deviceId: 'dev-1' }) }
  trackStops.push(track.stop)
  return { getAudioTracks: () => [track], getTracks: () => [track] }
}

/** Every frame handed to the socket, in order, so ordering can be asserted. */
type Sent = { kind: 'audio' | 'stop' | 'other'; raw: unknown }

const sockets: MockSocket[] = []
const lastSocket = () => sockets[sockets.length - 1]

class MockSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  readyState = 1
  binaryType = ''
  sent: Sent[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  deferClose = false
  private pendingClose: (() => void) | null = null
  constructor() {
    sockets.push(this)
    // The hook awaits `onopen` before it builds the audio graph, and a real
    // socket opens on a later task, not synchronously in the constructor.
    setTimeout(() => this.onopen?.(), 0)
  }
  send(payload: unknown) {
    if (typeof payload === 'string') {
      this.sent.push({ kind: payload.includes('"stop"') ? 'stop' : 'other', raw: payload })
    } else {
      this.sent.push({ kind: 'audio', raw: payload })
    }
  }
  close() {
    // A real socket fires `onclose` once. cleanup() also calls close(), so a
    // mock that re-fires would recurse through the hook's own close handler.
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    const fire = this.onclose
    this.onclose = null
    if (this.deferClose) this.pendingClose = fire
    else fire?.()
  }
  finishClose() { const fire = this.pendingClose; this.pendingClose = null; fire?.() }
  /** Backend finished starting Transcribe. */
  becomeReady(fields: Record<string, unknown> = {}) { this.onmessage?.({ data: JSON.stringify({ type: 'ready', ...fields }) }) }
  kinds() { return this.sent.map(s => s.kind) }
}

/** Captures the worklet node so a test can push PCM frames like the real one. */
const nodes: MockWorkletNode[] = []
const lastNode = () => nodes[nodes.length - 1]

class MockWorkletNode {
  port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null }
  constructor() { nodes.push(this) }
  connect() {}
  disconnect() {}
  /** One frame of captured audio, as the real pcm-worklet emits. */
  speak(bytes = 640) { this.port.onmessage?.({ data: new ArrayBuffer(bytes) }) }
}

class MockAudioContext {
  audioWorklet = { addModule: () => Promise.resolve() }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  trackStops.length = 0
  sockets.length = 0
  nodes.length = 0
  vi.stubGlobal('WebSocket', MockSocket as unknown as typeof WebSocket)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.stubGlobal('AudioWorkletNode', MockWorkletNode as unknown as typeof AudioWorkletNode)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue(makeStream()), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
})

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

async function startRecording() {
  const { useStreamingStt } = await import('../hooks/useStreamingStt')
  const onFinal = vi.fn()
  const onCaptureStop = vi.fn()
  const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal, onCaptureStop }))
  await act(async () => { hook.result.current.start() })
  await waitFor(() => expect(lastNode()).toBeTruthy())
  return { hook, onFinal, onCaptureStop }
}

describe('streaming stop before the server is ready', () => {
  it('sends the buffered speech BEFORE the stop frame', async () => {
    const { hook } = await startRecording()
    const ws = lastSocket()

    // The user speaks while Transcribe is still starting: three frames land in
    // the local buffer and nothing has gone out yet.
    await act(async () => { lastNode().speak(); lastNode().speak(); lastNode().speak() })
    expect(ws.kinds()).toEqual([])

    // Release the key -- still pre-`ready`.
    await act(async () => { hook.result.current.stop() })
    expect(ws.kinds(), 'stop must not out-run the audio').toEqual([])

    // Transcribe comes up. The buffer flushes, and only then does stop go out.
    await act(async () => { ws.becomeReady() })
    await waitFor(() => expect(ws.kinds()).toContain('stop'))

    expect(ws.kinds()).toEqual(['audio', 'audio', 'audio', 'stop'])
    expect(ws.kinds().indexOf('stop')).toBe(ws.sent.length - 1)
  })

  it('keeps a cold local model load\'s worth of speech, not just Transcribe\'s', async () => {
    // The local recogniser's first load is ~7.4s of GPU-pipeline compile after up to
    // ~4s of digest verification, and the cap used to be 8s of audio. Past it the
    // OLDEST frames go, so the user lost the words they opened with while the UI
    // showed a live mic. Twelve seconds of speech is inside the real wait and well
    // past the old ceiling.
    const { hook } = await startRecording()
    const ws = lastSocket()

    // 32 KB per second at 16 kHz mono Int16. One frame per second keeps the loop
    // cheap while still crossing the old cap by 50%.
    const ONE_SECOND = 32 * 1024
    await act(async () => {
      for (let i = 0; i < 12; i++) lastNode().speak(ONE_SECOND)
    })
    expect(ws.kinds(), 'nothing may ship before ready').toEqual([])

    await act(async () => { ws.becomeReady() })
    await waitFor(() => expect(ws.kinds().length).toBeGreaterThan(0))

    const shipped = ws.sent.filter(s => s.kind === 'audio')
    expect(shipped.length, 'the opening seconds were dropped').toBe(12)
    const bytes = shipped.reduce((n, s) => n + (s.raw as ArrayBuffer).byteLength, 0)
    expect(bytes).toBe(12 * ONE_SECOND)
    void hook
  })

  it('still stops immediately when the server is already ready', async () => {
    const { hook } = await startRecording()
    const ws = lastSocket()
    await act(async () => { ws.becomeReady() })
    await act(async () => { lastNode().speak() })
    expect(ws.kinds()).toEqual(['audio'])

    await act(async () => { hook.result.current.stop() })
    expect(ws.kinds()).toEqual(['audio', 'stop'])
  })

  it('auto-stops at sixty seconds and drains every opening frame plus the short tail', async () => {
    const { hook, onCaptureStop } = await startRecording()
    const ws = lastSocket()
    const node = lastNode()
    const postMessage = vi.fn()
    Object.assign(node.port, { postMessage })
    const captured: ArrayBuffer[] = []
    await act(async () => {
      for (let i = 0; i < 600; i++) {
        const frame = new ArrayBuffer(3200)
        new DataView(frame).setUint32(0, i, true)
        captured.push(frame)
        node.port.onmessage?.({ data: frame })
      }
    })
    expect(hook.result.current.recording).toBe(false)
    expect(hook.result.current.draining).toBe(true)
    expect(trackStops.at(-1)).toHaveBeenCalledOnce()
    expect(postMessage).toHaveBeenCalledOnce()
    expect(postMessage).toHaveBeenCalledWith({ type: 'flush' })
    expect(onCaptureStop).toHaveBeenCalledOnce()
    expect(ws.kinds()).toEqual([])

    const tail = new ArrayBuffer(192)
    await act(async () => {
      node.port.onmessage?.({ data: tail })
      node.port.onmessage?.({ data: { type: 'flushed' } as unknown as ArrayBuffer })
      node.speak()
      ws.becomeReady()
    })
    const audio = ws.sent.filter(frame => frame.kind === 'audio')
    expect(audio).toHaveLength(601)
    expect(audio.slice(0, 600).every((frame, i) => frame.raw === captured[i])).toBe(true)
    expect(audio[600].raw).toBe(tail)
    expect(audio.reduce((size, frame) => size + (frame.raw as ArrayBuffer).byteLength, 0)).toBe(60 * 16000 * 2 + 192)
    expect(ws.kinds().at(-1)).toBe('stop')
    expect(onCaptureStop).toHaveBeenCalledOnce()
  })

  it('stops capturing at release — post-release speech never ships', async () => {
    const { hook } = await startRecording()
    const ws = lastSocket()
    const node = lastNode()

    await act(async () => { node.speak(); node.speak() })
    await act(async () => { hook.result.current.stop() })

    // The room keeps making noise between release and `ready`. Deferring the
    // stop FRAME must not defer the end of CAPTURE, or those frames ride out on
    // the flush and get transcribed as words the user never meant to say.
    await act(async () => { node.speak(); node.speak(); node.speak() })
    await act(async () => { ws.becomeReady() })
    await waitFor(() => expect(ws.kinds()).toContain('stop'))

    expect(ws.kinds()).toEqual(['audio', 'audio', 'stop'])
  })

  it('releases the mic when `ready` never arrives', async () => {
    vi.useFakeTimers()
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn() }))

    // Fake timers mean the mock socket's own `onopen` has to be driven too;
    // `...Async` also flushes the microtasks the hook awaits.
    await act(async () => { hook.result.current.start() })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const ws = lastSocket()
    expect(lastNode()).toBeTruthy()

    await act(async () => { lastNode().speak() })
    await act(async () => { hook.result.current.stop() })

    // Deferring the stop must not mean deferring it forever: a backend that
    // accepts the socket and never answers would otherwise hold the mic open.
    expect(hook.result.current.recording).toBe(false)
    expect(hook.result.current.draining).toBe(true)
    expect(ws.readyState).toBe(MockSocket.OPEN)

    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(hook.result.current.recording).toBe(false)
    expect(ws.readyState).toBe(MockSocket.CLOSED)
  })

  it('drains the final short worklet frame before stop and stops capture immediately', async () => {
    const { hook } = await startRecording()
    const ws = lastSocket()
    const node = lastNode()
    await act(async () => { ws.becomeReady() })
    const postMessage = vi.fn()
    Object.assign(node.port, { postMessage })
    await act(async () => { node.speak(); hook.result.current.stop() })
    expect(hook.result.current.recording).toBe(false)
    expect(hook.result.current.draining).toBe(true)
    expect(postMessage).toHaveBeenCalledWith({ type: 'flush' })
    expect(ws.kinds()).toEqual(['audio'])
    await act(async () => {
      node.speak(320)
      node.port.onmessage?.({ data: { type: 'flushed' } as unknown as ArrayBuffer })
      node.speak()
    })
    expect(ws.kinds()).toEqual(['audio', 'audio', 'stop'])
    expect((ws.sent[1].raw as ArrayBuffer).byteLength).toBe(320)
  })

  it('keeps the unfinished utterance after an earlier final when the connection drops', async () => {
    const { hook, onFinal } = await startRecording()
    const ws = lastSocket()
    await act(async () => {
      ws.becomeReady()
      ws.onmessage?.({ data: JSON.stringify({ type: 'final', text: '你好。' }) })
      ws.onmessage?.({ data: JSON.stringify({ type: 'partial', text: '请继续' }) })
      ws.close()
    })
    expect(onFinal).toHaveBeenCalledWith('你好。请继续')
    expect(hook.result.current.recording).toBe(false)
  })

  it('does not resurrect an unstable hypothesis rejected by an empty final', async () => {
    const { onFinal } = await startRecording()
    const ws = lastSocket()
    await act(async () => {
      ws.becomeReady()
      ws.onmessage?.({ data: JSON.stringify({ type: 'final', text: '请保留真实内容。' }) })
      ws.onmessage?.({ data: JSON.stringify({ type: 'partial', text: '谢谢观看字幕' }) })
      ws.onmessage?.({ data: JSON.stringify({ type: 'final', text: '' }) })
      ws.close()
    })
    expect(onFinal).toHaveBeenCalledWith('请保留真实内容。')
  })

  it('preserves a short utterance released while the socket is still connecting', async () => {
    const { hook } = await startRecording()
    const ws = lastSocket()
    ws.readyState = MockSocket.CONNECTING
    await act(async () => { lastNode().speak(320); hook.result.current.stop() })
    expect(ws.kinds()).toEqual([])
    expect(hook.result.current.draining).toBe(true)
    await act(async () => { ws.readyState = MockSocket.OPEN; ws.becomeReady() })
    expect(ws.kinds()).toEqual(['audio', 'stop'])
  })

  it('a cancel during the wait does not later stop an abandoned session', async () => {
    const { hook } = await startRecording()
    const ws = lastSocket()
    await act(async () => { lastNode().speak() })
    await act(async () => { hook.result.current.stop() })
    await act(async () => { hook.result.current.cancel() })

    // cancel() tears the socket down; a `ready` that arrives afterwards must not
    // resurrect the pending stop and ship audio the user discarded.
    await act(async () => { ws.becomeReady() })
    expect(ws.kinds()).not.toContain('stop')
  })

  it('keeps active-session cancel as a discard without a capture-stop delivery', async () => {
    const { hook, onCaptureStop, onFinal } = await startRecording()
    await act(async () => { lastNode().speak(); hook.result.current.cancel() })

    expect(onCaptureStop).not.toHaveBeenCalled()
    expect(onFinal).not.toHaveBeenCalled()
  })
})

describe('final decoding has its own negotiated deadline', () => {
  async function startDraining(fields: Record<string, unknown> = {}) {
    vi.useFakeTimers()
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    const onFinal = vi.fn()
    const onError = vi.fn()
    const onCaptureStop = vi.fn()
    const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal, onError, onCaptureStop }))
    await act(async () => { await hook.result.current.start() })
    const ws = lastSocket()
    await act(async () => { ws.becomeReady(fields) })
    await act(async () => { lastNode().speak(); hook.result.current.stop() })
    return { hook, ws, onFinal, onError, onCaptureStop }
  }

  it('accepts a valid CPU final at eighty-five seconds under the server deadline', async () => {
    const { hook, ws, onFinal, onError } = await startDraining({ final_timeout_ms: 120000 })
    await act(async () => { await vi.advanceTimersByTimeAsync(85000) })
    expect(ws.readyState).toBe(MockSocket.OPEN)
    expect(hook.result.current.recording).toBe(false)
    expect(hook.result.current.draining).toBe(true)
    expect(onError).not.toHaveBeenCalled()
    await act(async () => {
      ws.onmessage?.({ data: JSON.stringify({ type: 'final', text: '完整的最终识别结果' }) })
      ws.close()
    })
    expect(onFinal).toHaveBeenCalledWith('完整的最终识别结果')
    expect(hook.result.current.draining).toBe(false)
    await act(async () => { await vi.advanceTimersByTimeAsync(40000) })
    expect(onError).not.toHaveBeenCalled()
  })

  it('still closes a hung session when the negotiated final budget expires', async () => {
    const { hook, ws, onError } = await startDraining({ final_timeout_ms: 120000 })
    await act(async () => { await vi.advanceTimersByTimeAsync(119999) })
    expect(hook.result.current.draining).toBe(true)
    expect(onError).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(hook.result.current.draining).toBe(false)
    expect(onError).toHaveBeenCalledOnce()
  })

  it.each([undefined, -1, '120000', 2147483648])('uses a bounded legacy fallback for an absent or invalid budget: %s', async budget => {
    const { hook, ws, onError } = await startDraining({ final_timeout_ms: budget })
    await act(async () => { await vi.advanceTimersByTimeAsync(314999) })
    expect(ws.readyState).toBe(MockSocket.OPEN)
    expect(hook.result.current.draining).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(onError).toHaveBeenCalledOnce()
  })

  it('cancels immediately during a long final decode without a later timeout error', async () => {
    const { hook, ws, onFinal, onError, onCaptureStop } = await startDraining({ final_timeout_ms: 625000 })
    await act(async () => { await vi.advanceTimersByTimeAsync(85000); hook.result.current.cancel() })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(hook.result.current.draining).toBe(false)
    await act(async () => { await vi.advanceTimersByTimeAsync(625000) })
    expect(onFinal).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
    expect(onCaptureStop).toHaveBeenCalledOnce()
  })
})

/**
 * A failed decode after the user presses stop must SAY so.
 *
 * The backend answers a failed whole-buffer decode with an `error` frame carrying
 * `stt_decode_failed` and then closes. Before that code existed the socket simply
 * closed with no frame at all, so the hook cleared the partial and the dictation
 * disappeared with nothing on screen — indistinguishable from a silent room.
 */
describe('streaming errors reach the user', () => {
  async function startWithHandlers () {
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    const onError = vi.fn()
    const onPartial = vi.fn()
    const onFinal = vi.fn()
    const hook = renderHook(() => useStreamingStt({ onPartial, onFinal, onError }))
    await act(async () => { hook.result.current.start() })
    await waitFor(() => expect(lastNode()).toBeTruthy())
    return { hook, onError, onPartial, onFinal }
  }

  it('renders localised text keyed off `code`, not the English `message`', async () => {
    const { unavailableMessage } = await import('../lib/sttProviders')
    const { onError } = await startWithHandlers()
    const ws = lastSocket()
    await act(async () => { ws.becomeReady() })

    await act(async () => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: 'error',
          message: 'the speech recogniser could not finish this transcript',
          code: 'stt_decode_failed',
        }),
      })
    })

    expect(onError).toHaveBeenCalledTimes(1)
    const shown = onError.mock.calls[0][0]
    // The backend's own sentence is advisory; a 12-language UI cannot render it.
    expect(shown).not.toBe('the speech recogniser could not finish this transcript')
    // And it is the DECODE reason, not a generic failure or the settings panel's
    // model-download copy.
    expect(shown).not.toBe(unavailableMessage('stt_model_missing'))
    expect(shown.length).toBeGreaterThan(0)
  })

  it('keeps the error visible when the backend closes right after it', async () => {
    const { onError, onFinal } = await startWithHandlers()
    const ws = lastSocket()
    await act(async () => { ws.becomeReady() })
    await act(async () => {
      ws.onmessage?.({ data: JSON.stringify({ type: 'error', message: 'x', code: 'stt_decode_failed' }) })
    })
    const shown = onError.mock.calls[0][0]

    // The close carries no finals, which is the state the hook used to treat as
    // "nothing was said". It may clear the partial; it must not retract the error or
    // commit a transcript that does not exist.
    await act(async () => { ws.close() })
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0][0]).toBe(shown)
    expect(onFinal).not.toHaveBeenCalled()
  })

  it('releases the mic on a fatal frame without waiting for the native decoder to close', async () => {
    const { hook, onError, onFinal } = await startWithHandlers()
    const ws = lastSocket()
    await act(async () => {
      ws.becomeReady()
      ws.onmessage?.({ data: JSON.stringify({ type: 'partial', text: '请保留这些话' }) })
      ws.onmessage?.({ data: JSON.stringify({ type: 'error', code: 'stt_decode_failed' }) })
    })
    expect(trackStops.at(-1)).toHaveBeenCalled()
    expect(hook.result.current.recording).toBe(false)
    expect(hook.result.current.draining).toBe(false)
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(onError).toHaveBeenCalledOnce()
    expect(onFinal).toHaveBeenCalledWith('请保留这些话')
  })

  it('reports capture ended once before a fatal frame waits for socket close', async () => {
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    const onCaptureStop = vi.fn()
    const onFinal = vi.fn()
    const hook = renderHook(() => useStreamingStt({
      onPartial: vi.fn(),
      onFinal,
      onCaptureStop,
      onError: vi.fn(),
    }))
    await act(async () => { await hook.result.current.start() })
    const ws = lastSocket()
    ws.deferClose = true
    await act(async () => {
      ws.becomeReady()
      ws.onmessage?.({ data: JSON.stringify({ type: 'partial', text: 'keep this' }) })
      ws.onmessage?.({ data: JSON.stringify({ type: 'error', code: 'stt_decode_failed' }) })
    })

    expect(onCaptureStop).toHaveBeenCalledOnce()
    expect(onFinal).not.toHaveBeenCalled()
    await act(async () => { ws.finishClose() })
    expect(onFinal).toHaveBeenCalledWith('keep this')
    expect(onCaptureStop).toHaveBeenCalledOnce()
  })

  it('falls back to a generic message when the frame carries no known code', async () => {
    const { onError } = await startWithHandlers()
    const ws = lastSocket()
    await act(async () => { ws.becomeReady() })
    await act(async () => {
      ws.onmessage?.({ data: JSON.stringify({ type: 'error' }) })
    })
    // No code and no message: the user still gets a sentence rather than silence.
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0][0]).toBeTruthy()
  })
})
