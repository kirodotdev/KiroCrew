/**
 * The meetings live-transcription hook, driven end to end over mocked audio and
 * a mocked STT socket.
 *
 * `captionWindow` already has direct tests (`MeetingsSessionLogic.test.ts`); the
 * hook body did not, so everything here aims at the stateful half: the pre-`ready`
 * PCM buffer and its cap, the server frame handlers, the stall watchdog, the
 * deferred stop, and the dispatch retry ladder that is the only path a final
 * segment reaches the agents.
 *
 * Timers are faked for every test so nothing depends on real elapsed time, and
 * the socket / AudioContext / worklet doubles follow the harness in
 * `useStreamingStt.stopBeforeReady.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

type HookModule = typeof import('../apps/meetings/hooks/useMeetingTranscription')
type ApiModule = typeof import('../apps/meetings/api')

type Sent = { kind: 'audio' | 'stop' | 'other'; bytes: number }

const sockets: MockSocket[] = []
const lastSocket = () => sockets[sockets.length - 1]

class MockSocket {
  static readonly OPEN = 1
  static readonly CLOSED = 3
  /** Set by a test to make the NEXT socket report a failure to open. */
  static failNextOpen = false

  readyState = 1
  binaryType = ''
  readonly url: string
  sent: Sent[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    sockets.push(this)
    const fail = MockSocket.failNextOpen
    MockSocket.failNextOpen = false
    // A real socket opens on a later task, never inside the constructor.
    setTimeout(() => { if (fail) this.onerror?.(); else this.onopen?.() }, 0)
  }

  send(payload: unknown) {
    if (typeof payload === 'string') {
      this.sent.push({ kind: payload.includes('"stop"') ? 'stop' : 'other', bytes: payload.length })
    } else {
      this.sent.push({ kind: 'audio', bytes: (payload as ArrayBuffer).byteLength })
    }
  }

  close() {
    // cleanup() also calls close(), so a mock that re-fired would recurse
    // through the hook's own close handler.
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    const fire = this.onclose
    this.onclose = null
    fire?.()
  }

  emit(msg: unknown) { this.onmessage?.({ data: JSON.stringify(msg) }) }
  becomeReady() { this.emit({ type: 'ready' }) }
  kinds() { return this.sent.map(s => s.kind) }
  audioBytes() { return this.sent.filter(s => s.kind === 'audio').map(s => s.bytes) }
}

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

let workletFails = false

class MockAudioContext {
  static closed = 0
  audioWorklet = {
    addModule: () =>
      workletFails ? Promise.reject(new Error('no worklet')) : Promise.resolve(),
  }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { MockAudioContext.closed += 1; return Promise.resolve() }
}

const stoppedTracks: string[] = []

function makeStream(label = 'mic') {
  const track = {
    stop: () => { stoppedTracks.push(label) },
    readyState: 'live',
    getSettings: () => ({ deviceId: 'dev-1' }),
  }
  return { getAudioTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream
}

let getUserMedia: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetModules()
  sockets.length = 0
  nodes.length = 0
  stoppedTracks.length = 0
  workletFails = false
  MockSocket.failNextOpen = false
  MockAudioContext.closed = 0
  getUserMedia = vi.fn().mockResolvedValue(makeStream())
  vi.stubGlobal('WebSocket', MockSocket as unknown as typeof WebSocket)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.stubGlobal('AudioWorkletNode', MockWorkletNode as unknown as typeof AudioWorkletNode)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

/** Advance fake time and let every microtask the hook awaits settle. */
async function flush(ms = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

interface Harness {
  mod: HookModule
  api: ApiModule
  dispatch: ReturnType<typeof vi.fn>
  onCaption: ReturnType<typeof vi.fn>
  onError: ReturnType<typeof vi.fn>
  onFinal: ReturnType<typeof vi.fn>
  hook: ReturnType<typeof renderHook<ReturnType<HookModule['useMeetingTranscription']>, unknown>>
  captions: () => string[]
  errors: () => string[]
}

/**
 * Fresh module instances per test so `transcriptionSupported` — computed at
 * import time — sees this test's globals, and so the api object the hook closes
 * over is the same one the spy is installed on.
 */
async function mount(onFinalImpl?: (text: string) => string | boolean | void): Promise<Harness> {
  const api = await import('../apps/meetings/api')
  const mod = await import('../apps/meetings/hooks/useMeetingTranscription')
  const dispatch = vi.fn().mockResolvedValue({ dispatched: 1, text: 'ok' })
  vi.spyOn(api.meetingsApi, 'dispatch').mockImplementation(
    (id: string, text: string) => dispatch(id, text) as Promise<{ dispatched: number; text: string }>,
  )
  const onCaption = vi.fn()
  const onError = vi.fn()
  const onFinal = vi.fn(onFinalImpl)
  const hook = renderHook(() =>
    mod.useMeetingTranscription({ meetingId: 'meet-1', onCaption, onError, onFinal }),
  )
  return {
    mod,
    api,
    dispatch,
    onCaption,
    onError,
    onFinal,
    hook,
    captions: () => onCaption.mock.calls.map(c => String(c[0])),
    errors: () => onError.mock.calls.map(c => String(c[0])),
  }
}

/** Start capture and settle through getUserMedia, the socket open and the worklet. */
async function startCapture(h: Harness) {
  await act(async () => { void h.hook.result.current.start() })
  await flush()
  return lastSocket()
}

describe('useMeetingTranscription — starting up', () => {
  it('opens the STT socket, arms capture and reports itself active', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    expect(ws.url).toBe(`ws://${window.location.host}/api/ws/stt`)
    expect(ws.binaryType).toBe('arraybuffer')
    expect(h.hook.result.current.active).toBe(true)
    expect(h.hook.result.current.supported).toBe(true)
    expect(lastNode()).toBeTruthy()
    expect(h.errors()).toEqual([])
  })

  it('buffers PCM until the server is ready, then flushes it in order', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    await act(async () => { lastNode().speak(100); lastNode().speak(200) })
    expect(ws.kinds()).toEqual([])

    await act(async () => { ws.becomeReady() })
    await flush()
    expect(ws.audioBytes()).toEqual([100, 200])

    // Past `ready` frames go straight out instead of accumulating.
    await act(async () => { lastNode().speak(300) })
    expect(ws.audioBytes()).toEqual([100, 200, 300])
  })

  it('drops the OLDEST buffered frames once the pre-ready cap is passed', async () => {
    const h = await mount()
    const ws = await startCapture(h)
    const big = 200 * 1024 // two of these already exceed the ~262 KB cap

    await act(async () => { lastNode().speak(big); lastNode().speak(big); lastNode().speak(big) })
    await act(async () => { ws.becomeReady() })
    await flush()

    // The most recent speech wins: only the last frame survived the trim.
    expect(ws.audioBytes()).toEqual([big])
  })

  it('does not send live audio once the socket has closed', async () => {
    const h = await mount()
    const ws = await startCapture(h)
    await act(async () => { ws.becomeReady() })
    await flush()

    ws.readyState = MockSocket.CLOSED
    await act(async () => { lastNode().speak(640) })
    expect(ws.audioBytes()).toEqual([])
  })

  it('reports a microphone failure and stays inactive', async () => {
    const h = await mount()
    getUserMedia.mockRejectedValue(Object.assign(new Error('nope'), { name: 'NotAllowedError' }))
    await startCapture(h)

    expect(h.errors()).toEqual(['microphone'])
    expect(sockets).toHaveLength(0)
    expect(h.hook.result.current.active).toBe(false)
  })

  it('reports a socket that never opens and releases the microphone', async () => {
    const h = await mount()
    MockSocket.failNextOpen = true
    await startCapture(h)

    // The teardown closes the half-open socket, whose close handler also fires,
    // so the failure is reported twice — the first message is the cause.
    expect(h.errors()).toEqual(['connection', 'disconnected'])
    expect(h.hook.result.current.active).toBe(false)
    expect(stoppedTracks).toEqual(['mic'])
    expect(lastNode()).toBeUndefined()
  })

  it('reports a missing audio worklet module', async () => {
    const h = await mount()
    workletFails = true
    await startCapture(h)

    expect(h.errors()).toEqual(['worklet', 'disconnected'])
    expect(h.hook.result.current.active).toBe(false)
    expect(MockAudioContext.closed).toBe(1)
  })

  it('surfaces a post-open transport error without tearing capture down', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    await act(async () => { ws.onerror?.() })
    expect(h.errors()).toEqual(['connection'])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('ignores a second start while one socket is already live', async () => {
    const h = await mount()
    await startCapture(h)
    await startCapture(h)

    expect(sockets).toHaveLength(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('collapses two starts that race inside the await window', async () => {
    const h = await mount()
    await act(async () => {
      void h.hook.result.current.start()
      void h.hook.result.current.start()
    })
    await flush()

    // Two microphone streams and two sockets would dispatch every final twice.
    expect(sockets).toHaveLength(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('reports unsupported when the browser has no audio worklet', async () => {
    vi.stubGlobal('AudioWorkletNode', undefined)
    vi.resetModules()
    const h = await mount()
    expect(h.mod.transcriptionSupported).toBe(false)

    await act(async () => { await h.hook.result.current.start() })
    expect(h.errors()).toEqual(['unsupported'])
    expect(h.hook.result.current.supported).toBe(false)
    expect(getUserMedia).not.toHaveBeenCalled()
  })
})

describe('useMeetingTranscription — server frames', () => {
  it('drives the caption from partials and commits finals', async () => {
    const h = await mount()
    const ws = await startCapture(h)
    await act(async () => { ws.becomeReady() })
    await flush()

    await act(async () => { ws.emit({ type: 'partial', text: 'hello wor' }) })
    expect(h.captions()).toEqual(['hello wor'])

    await act(async () => { ws.emit({ type: 'final', text: '  hello world  ' }) })
    expect(h.captions()).toEqual(['hello wor', 'hello world'])

    // A later partial is appended to the committed finals, not shown alone.
    await act(async () => { ws.emit({ type: 'partial', text: 'and then' }) })
    expect(h.captions().at(-1)).toBe('hello world and then')

    await flush()
    expect(h.dispatch).toHaveBeenCalledWith('meet-1', 'hello world')
  })

  it('ignores an empty final and a partial with no text', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    await act(async () => { ws.emit({ type: 'final', text: '   ' }) })
    expect(h.onFinal).not.toHaveBeenCalled()
    expect(h.dispatch).not.toHaveBeenCalled()

    await act(async () => { ws.emit({ type: 'partial' }) })
    expect(h.captions()).toEqual([''])
  })

  it('ignores binary frames and unparseable text frames', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    await act(async () => {
      ws.onmessage?.({ data: new ArrayBuffer(8) })
      ws.onmessage?.({ data: 'not json at all' })
      ws.onmessage?.({ data: JSON.stringify({ type: 'unknown-kind' }) })
    })

    expect(h.captions()).toEqual([])
    expect(h.errors()).toEqual([])
    expect(h.dispatch).not.toHaveBeenCalled()
  })

  it('suppresses the dispatch when the caller rejects the segment', async () => {
    const h = await mount(() => false)
    const ws = await startCapture(h)

    await act(async () => { ws.emit({ type: 'final', text: 'duplicate line' }) })
    await flush()

    // A rejected final still belongs in the caption, just not with the agents.
    expect(h.captions()).toEqual(['duplicate line'])
    expect(h.dispatch).not.toHaveBeenCalled()
  })

  it('dispatches only the suffix the caller hands back', async () => {
    const h = await mount(() => 'please')
    const ws = await startCapture(h)

    await act(async () => { ws.emit({ type: 'final', text: 'yes please' }) })
    await flush()

    expect(h.dispatch).toHaveBeenCalledWith('meet-1', 'please')
  })

  it('skips a dispatch when the caller returns a blank suffix', async () => {
    const h = await mount(() => '   ')
    const ws = await startCapture(h)

    await act(async () => { ws.emit({ type: 'final', text: 'already sent' }) })
    await flush()

    expect(h.captions()).toEqual(['already sent'])
    expect(h.dispatch).not.toHaveBeenCalled()
  })

  it('reports a server error frame and unblocks the start', async () => {
    const h = await mount()
    let settled = false
    await act(async () => { void h.hook.result.current.start().then(() => { settled = true }) })
    await flush()
    const ws = lastSocket()

    // `start` is still awaiting the server's `ready` gate at this point.
    expect(settled).toBe(false)

    await act(async () => { ws.emit({ type: 'error', message: 'model unavailable' }) })
    await flush()
    expect(h.errors()).toEqual(['model unavailable'])
    // An error resolves the same gate, so a backend that fails to start its
    // stream does not leave `start` hanging forever.
    expect(settled).toBe(true)

    await act(async () => { ws.emit({ type: 'error' }) })
    expect(h.errors()).toEqual(['model unavailable', 'error'])
  })

  it('reports an unexpected close as a disconnect', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    await act(async () => { ws.close() })
    expect(h.errors()).toEqual(['disconnected'])
    expect(h.hook.result.current.active).toBe(false)
    expect(stoppedTracks).toEqual(['mic'])
  })

  it('ignores the close of a socket that has already been replaced', async () => {
    const h = await mount()
    const first = await startCapture(h)
    const staleClose = first.onclose
    expect(staleClose).toBeTruthy()

    // A stall reconnect installs a NEW socket; the old close lands afterwards.
    await act(async () => { await vi.advanceTimersByTimeAsync(25_000) })
    await flush()
    expect(sockets).toHaveLength(2)
    const errorsBefore = h.errors().length

    await act(async () => { staleClose?.() })
    expect(h.errors()).toHaveLength(errorsBefore)
    expect(h.hook.result.current.active).toBe(true)
  })
})

describe('useMeetingTranscription — watchdog and stop', () => {
  it('reconnects when no server frame has arrived for the stall window', async () => {
    const h = await mount()
    await startCapture(h)

    await act(async () => { await vi.advanceTimersByTimeAsync(25_000) })
    await flush()

    expect(sockets).toHaveLength(2)
    expect(h.hook.result.current.active).toBe(true)
  })

  it('leaves a socket alone while frames keep arriving', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    for (let i = 0; i < 5; i += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
      await act(async () => { ws.emit({ type: 'partial', text: `chunk ${i}` }) })
    }

    expect(sockets).toHaveLength(1)
    expect(h.errors()).toEqual([])
  })

  it('sends the stop frame and lets the server close, forcing it after the grace', async () => {
    const h = await mount()
    const ws = await startCapture(h)
    await act(async () => { ws.becomeReady() })
    await flush()

    await act(async () => { h.hook.result.current.stop() })
    expect(ws.kinds()).toEqual(['stop'])
    // Still up, so a trailing final can arrive after the stop frame.
    expect(ws.readyState).toBe(MockSocket.OPEN)

    await act(async () => { ws.emit({ type: 'final', text: 'trailing words' }) })
    await flush()
    expect(h.dispatch).toHaveBeenCalledWith('meet-1', 'trailing words')

    await act(async () => { await vi.advanceTimersByTimeAsync(8_000) })
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(h.hook.result.current.active).toBe(false)
    // A stop we asked for is not reported as a disconnect.
    expect(h.errors()).toEqual([])
  })

  it('cleans up immediately when there is no open socket to stop', async () => {
    const h = await mount()

    await act(async () => { h.hook.result.current.stop() })
    expect(h.hook.result.current.active).toBe(false)
    expect(sockets).toHaveLength(0)
  })

  it('the grace timer never tears down a socket that replaced the stopped one', async () => {
    const h = await mount()
    const first = await startCapture(h)
    await act(async () => { first.becomeReady() })
    await flush()

    await act(async () => { h.hook.result.current.stop() })
    await act(async () => { first.close() })
    const second = await startCapture(h)
    expect(second).not.toBe(first)

    await act(async () => { await vi.advanceTimersByTimeAsync(8_000) })
    expect(second.readyState).toBe(MockSocket.OPEN)
    expect(h.hook.result.current.active).toBe(true)
  })

  it('releases the microphone when the component unmounts', async () => {
    const h = await mount()
    const ws = await startCapture(h)

    h.hook.unmount()
    expect(ws.readyState).toBe(MockSocket.CLOSED)
    expect(stoppedTracks).toEqual(['mic'])
    expect(MockAudioContext.closed).toBe(1)
  })
})

describe('useMeetingTranscription — dispatch retries', () => {
  async function sendFinal(h: Harness, text = 'a spoken segment') {
    const ws = await startCapture(h)
    await act(async () => { ws.becomeReady() })
    await flush()
    await act(async () => { ws.emit({ type: 'final', text }) })
    return ws
  }

  it('retries a segment the server explicitly rejected, then succeeds', async () => {
    const h = await mount()
    const rejected = new h.api.MeetingsApiError('bad gateway', 502)
    h.dispatch.mockRejectedValueOnce(rejected).mockResolvedValue({ dispatched: 1, text: 'ok' })

    await sendFinal(h)
    expect(h.dispatch).toHaveBeenCalledTimes(1)

    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(h.dispatch).toHaveBeenCalledTimes(2)
    expect(h.errors()).toEqual([])
  })

  it('gives up after the retry ladder and reports the lost segment', async () => {
    const h = await mount()
    h.dispatch.mockRejectedValue(new h.api.MeetingsApiError('server error', 500))

    await sendFinal(h)
    for (const delay of [400, 1_200, 3_000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(delay) })
    }
    await flush()

    // Four attempts, then the caller is told rather than the gap being silent.
    expect(h.dispatch).toHaveBeenCalledTimes(4)
    expect(h.errors()).toEqual(['dispatch'])
  })

  it('never retries an ambiguous failure, because the segment may have landed', async () => {
    const h = await mount()
    h.dispatch.mockRejectedValue(new TypeError('network down'))

    await sendFinal(h)
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })

    expect(h.dispatch).toHaveBeenCalledTimes(1)
    expect(h.errors()).toEqual(['dispatch'])
  })

  it('keeps the stream alive after a dispatch failure', async () => {
    const h = await mount()
    h.dispatch.mockRejectedValueOnce(new TypeError('network down'))

    const ws = await sendFinal(h, 'first segment')
    await flush()
    expect(h.errors()).toEqual(['dispatch'])
    expect(h.hook.result.current.active).toBe(true)

    await act(async () => { ws.emit({ type: 'final', text: 'second segment' }) })
    await flush()
    expect(h.dispatch).toHaveBeenLastCalledWith('meet-1', 'second segment')
  })
})
