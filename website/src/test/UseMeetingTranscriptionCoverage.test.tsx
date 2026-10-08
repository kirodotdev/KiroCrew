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
/** When set, the next addModule() returns this promise (for ordering tests). */
let nextWorkletLoad: Promise<void> | null = null

class MockAudioContext {
  static closed = 0
  audioWorklet = {
    addModule: () => {
      if (nextWorkletLoad) {
        const p = nextWorkletLoad
        nextWorkletLoad = null
        return p
      }
      return workletFails ? Promise.reject(new Error('no worklet')) : Promise.resolve()
    },
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
  nextWorkletLoad = null
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
  partials: () => string[]
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
  const onPartial = vi.fn()
  const hook = renderHook(() =>
    mod.useMeetingTranscription({ meetingId: 'meet-1', onCaption, onError, onFinal, onPartial }),
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
    partials: () => onPartial.mock.calls.map(c => String(c[0])),
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
    await act(async () => { ws.becomeReady() })

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
    await act(async () => { ws.becomeReady() })

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

  it.each(['start-before-close', 'close-before-start'] as const)(
    'stays down after an error frame before ready (%s)',
    async order => {
      const h = await mount()
      const ws = await startCapture(h)

      await act(async () => { ws.emit({ type: 'error', code: 'stt_streaming_disabled' }) })
      expect(h.errors()).toEqual(['stt_streaming_disabled'])
      expect(h.hook.result.current.active).toBe(false)
      expect(h.hook.result.current.connecting).toBe(false)
      expect(h.hook.result.current.halted).toBe(true)

      // The caller's effect re-runs on the connecting edge and calls start();
      // the server's close can land before or after it. Neither order reopens.
      if (order === 'close-before-start') await act(async () => { ws.close() })
      await act(async () => { await h.hook.result.current.start() })
      if (order === 'start-before-close') await act(async () => { ws.close() })
      await act(async () => { await h.hook.result.current.start() })
      await flush()

      expect(sockets).toHaveLength(1)
      // The server's own close is not reported as a second, generic error.
      expect(h.errors()).toEqual(['stt_streaming_disabled'])

      // stop() (pause or end) clears the halt; the next start retries once.
      await act(async () => { h.hook.result.current.stop() })
      expect(h.hook.result.current.halted).toBe(false)
      await startCapture(h)
      expect(sockets).toHaveLength(2)
    },
  )

  it.each([
    ['after ready', true],
    ['before ready', false],
  ] as const)('reconnects after a retryable error frame (%s)', async (_label, readyFirst) => {
    const h = await mount()
    const ws = await startCapture(h)
    if (readyFirst) await act(async () => { ws.becomeReady() })

    // The 5-minute stream cap: the server sends this frame, then closes.
    await act(async () => { ws.emit({ type: 'error', code: 'stt_max_duration_exceeded' }) })
    expect(h.hook.result.current.halted).toBe(false)
    // `connecting`/`active` stay up until the close, so the caller's effect
    // re-runs only once the old socket is gone.
    expect(h.hook.result.current.active || h.hook.result.current.connecting).toBe(true)

    await act(async () => { ws.close() })
    expect(h.hook.result.current.active).toBe(false)
    expect(h.hook.result.current.connecting).toBe(false)
    // No second, generic error for the server's own close.
    expect(h.errors()).toEqual(['stt_max_duration_exceeded'])

    await startCapture(h)
    expect(sockets).toHaveLength(2)
  })

  it('never reports active when ready arrives before a worklet load that fails', async () => {
    const h = await mount()
    let rejectLoad: (e: Error) => void = () => {}
    nextWorkletLoad = new Promise<void>((_, reject) => { rejectLoad = reject })
    const seen: boolean[] = []
    await act(async () => { void h.hook.result.current.start() })
    await flush()
    const ws = lastSocket()

    await act(async () => { ws.becomeReady() })
    seen.push(h.hook.result.current.active)
    await act(async () => { rejectLoad(new Error('404')) })
    await flush()
    seen.push(h.hook.result.current.active)

    // `active` never flips true→false, so the session effect sees no drop.
    expect(seen).toEqual([false, false])
    expect(h.errors()).toContain('worklet')
  })

  it('reports active once the worklet is set up even if ready came first', async () => {
    const h = await mount()
    let resolveLoad: () => void = () => {}
    nextWorkletLoad = new Promise<void>(resolve => { resolveLoad = resolve })
    await act(async () => { void h.hook.result.current.start() })
    await flush()
    const ws = lastSocket()

    await act(async () => { ws.becomeReady() })
    expect(h.hook.result.current.active).toBe(false)
    await act(async () => { resolveLoad() })
    await flush()
    expect(h.hook.result.current.active).toBe(true)
    expect(h.hook.result.current.connecting).toBe(false)
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
    await act(async () => { lastSocket().becomeReady() })
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
    await act(async () => { lastSocket().becomeReady() })
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
    await act(async () => { second.becomeReady() })

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

describe('useMeetingTranscription — lease rotation before the server cap', () => {
  async function readyWith(ws: MockSocket, extra: Record<string, unknown> = {}) {
    await act(async () => { ws.emit({ type: 'ready', ...extra }) })
    await flush()
  }

  /** Advance time in steps short enough to keep the stall watchdog quiet. */
  async function keepAlive(ws: MockSocket, ms: number) {
    for (let t = 0; t < ms; t += 15_000) {
      await act(async () => { ws.emit({ type: 'partial', text: 'still talking' }) })
      await flush(Math.min(15_000, ms - t))
    }
  }

  it('opens the next socket ROTATE_LEAD_MS before the advertised cap, keeping the microphone', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })

    await flush(9_000)
    expect(sockets).toHaveLength(1)
    await flush(1_000) // 40 s cap − 30 s lead
    expect(sockets).toHaveLength(2)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(stoppedTracks).toEqual([])
    expect(nodes).toHaveLength(1)
    expect(h.errors()).toEqual([])
  })

  it('routes audio to the successor once it is ready and stops the predecessor', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    expect(b).not.toBe(a)

    await act(async () => { lastNode().speak(100) })
    expect(a.audioBytes()).toEqual([100])
    expect(b.audioBytes()).toEqual([])

    await readyWith(b, { max_duration_ms: 40_000 })
    expect(a.kinds()).toContain('stop')
    await act(async () => { lastNode().speak(200) })
    expect(b.audioBytes()).toEqual([200])
    expect(a.audioBytes()).toEqual([100])
    expect(h.hook.result.current.active).toBe(true)
    expect(h.errors()).toEqual([])
  })

  it("keeps the predecessor's trailing final ahead of the successor's, and its close is silent", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => { b.emit({ type: 'final', text: 'after the switch' }) })
    await flush()
    expect(h.dispatch).not.toHaveBeenCalled()

    await act(async () => { a.emit({ type: 'final', text: 'before the switch' }) })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['before the switch'])

    await act(async () => { a.close() })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['before the switch', 'after the switch'])
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('releases held finals when the drain grace expires without a close', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    // The successor's own cap is far off, so no rotation of its own comes due
    // inside the grace: the predecessor's socket is left to the server.
    await readyWith(b, { max_duration_ms: 300_000 })
    await act(async () => { b.emit({ type: 'final', text: 'held' }) })
    await flush()
    expect(h.dispatch).not.toHaveBeenCalled()

    await keepAlive(b, 60_000) // DRAIN_GRACE_MS, with the watchdog kept quiet
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['held'])
    // The socket stays open for the server to finish decoding: a client close
    // would make it discard the final. When that final does land, it is dispatched.
    expect(a.readyState).toBe(MockSocket.OPEN)
    await act(async () => { a.emit({ type: 'final', text: 'late tail' }) })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['held', 'late tail'])
    await act(async () => { a.close() })
    await flush()
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it("a draining predecessor's final clears the stale partial it left on screen", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await act(async () => { a.emit({ type: 'partial', text: 'hello wor' }) })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })
    expect(h.partials().at(-1)).toBe('hello wor')

    await act(async () => { a.emit({ type: 'final', text: 'hello world' }) })
    await flush()
    // The live lease has nothing in flight, so the live row is cleared.
    expect(h.partials().at(-1)).toBe('')
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['hello world'])
  })

  it('runs the dedup in dispatch order, so held finals are judged after the tail they waited for', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => { b.emit({ type: 'final', text: 'after the switch' }) })
    await act(async () => { a.emit({ type: 'final', text: 'tail of the old one' }) })
    await flush()
    expect(h.onFinal.mock.calls.map(c => c[0])).toEqual(['tail of the old one'])

    await act(async () => { a.close() })
    await flush()
    expect(h.onFinal.mock.calls.map(c => c[0])).toEqual(['tail of the old one', 'after the switch'])
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail of the old one', 'after the switch'])
  })

  it('a live final arriving as the hold is released still queues behind every held final', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => {
      b.emit({ type: 'final', text: 'held one' })
      b.emit({ type: 'final', text: 'held two' })
      a.emit({ type: 'final', text: 'tail' })
    })
    // The drain ends and, in the same breath, the live lease commits again:
    // release order must not be overtaken by the arrival.
    await act(async () => {
      a.close()
      b.emit({ type: 'final', text: 'live after release' })
    })
    await flush()
    const order = ['tail', 'held one', 'held two', 'live after release']
    expect(h.onFinal.mock.calls.map(c => c[0])).toEqual(order)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(order)
  })

  it("the successor's takeover resets the stall watchdog", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    expect(b).not.toBe(a)

    // The predecessor falls silent while the successor connects, past
    // STALL_TIMEOUT_MS by the next watchdog tick; the successor's `ready` lands
    // about a second before that tick.
    await flush(14_000)
    expect(sockets).toHaveLength(2)
    await readyWith(b, { max_duration_ms: 40_000 })
    expect(a.kinds()).toContain('stop')
    await flush(2_000)

    // The fresh lease is not mistaken for a stalled one.
    expect(sockets).toHaveLength(2)
    expect(b.readyState).toBe(MockSocket.OPEN)
    expect(a.readyState).toBe(MockSocket.OPEN)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(stoppedTracks).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
    await act(async () => { lastNode().speak(100) })
    expect(b.audioBytes()).toEqual([100])
    expect(h.errors()).toEqual([])
  })

  it('an unmount abandons a draining predecessor along with everything else', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })
    expect(a.readyState).toBe(MockSocket.OPEN)

    h.hook.unmount()
    expect(a.readyState).toBe(MockSocket.CLOSED)
    expect(b.readyState).toBe(MockSocket.CLOSED)
    expect(stoppedTracks).toEqual(['mic'])
  })

  it('uses the 300 s fallback when the server does not advertise its cap', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a)
    await keepAlive(a, 269_000)
    expect(sockets).toHaveLength(1)
    await keepAlive(a, 1_000)
    expect(sockets).toHaveLength(2)
  })

  it('derives the rotation lead from the advertised drain bound, so a drain the server allows ends before the cap', async () => {
    const h = await mount()
    const a = await startCapture(h)
    // The server says how long a stop's drain may take; the lead covers that
    // bound plus the successor's connect allowance: 300 s − (50 + 5) s.
    await readyWith(a, { final_timeout_ms: 50_000 })
    await keepAlive(a, 244_000)
    expect(sockets).toHaveLength(1)
    await keepAlive(a, 1_000)
    expect(sockets).toHaveLength(2)
    expect(h.errors()).toEqual([])
  })

  it('keeps the 30 s lead when the advertised drain bound is shorter', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000, final_timeout_ms: 10_000 })
    await flush(9_000)
    expect(sockets).toHaveLength(1)
    await flush(1_000) // 40 s cap − 30 s lead, not 40 − 15
    expect(sockets).toHaveLength(2)
  })

  it('caps the lead at half the cap when the advertised drain bound would not fit', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 100_000, final_timeout_ms: 400_000 })
    await keepAlive(a, 49_000)
    expect(sockets).toHaveLength(1)
    await keepAlive(a, 1_000) // half of the 100 s cap
    expect(sockets).toHaveLength(2)
  })

  it("held finals wait for the predecessor's trailing dispatch to settle, not just to be issued", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    // The server rejects the trailing segment once, so its dispatch is still
    // inside its retry ladder when the predecessor's close lands.
    h.dispatch.mockImplementationOnce(() => Promise.reject(new h.api.MeetingsApiError('busy', 503)))
    await act(async () => { a.emit({ type: 'final', text: 'tail' }) })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail'])

    await act(async () => { b.emit({ type: 'final', text: 'held one' }) })
    await act(async () => { a.close() })
    await flush(100)
    // The close alone releases nothing: the tail has not landed, and a final
    // arriving now still queues behind it.
    await act(async () => { b.emit({ type: 'final', text: 'held two' }) })
    await flush(100)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail'])

    await flush(400) // the retry lands
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail', 'tail', 'held one', 'held two'])
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('a trailing dispatch that never settles releases the held finals at the drain grace', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })
    h.dispatch.mockImplementationOnce(() => new Promise(() => {}))
    await act(async () => { a.emit({ type: 'final', text: 'tail' }) })
    await act(async () => { b.emit({ type: 'final', text: 'held' }) })
    await act(async () => { a.close() })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail'])

    await keepAlive(b, 60_000) // DRAIN_GRACE_MS, counted from the switch
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail', 'held'])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('held finals wait for every outstanding predecessor dispatch, not only the last one', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    // Two predecessor finals are outstanding when its close lands: the earlier
    // one is rejected once and retries at 400 ms, the later one is queued
    // behind it.
    h.dispatch.mockImplementationOnce(() => Promise.reject(new h.api.MeetingsApiError('busy', 503)))
    await act(async () => {
      a.emit({ type: 'final', text: 'first' })
      a.emit({ type: 'final', text: 'second' })
    })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['first'])

    await act(async () => { b.emit({ type: 'final', text: 'held' }) })
    await act(async () => { a.close() })
    await flush(100)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['first'])

    await flush(400) // the retry lands, then the second, and only then the held one
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['first', 'first', 'second', 'held'])
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })
  it('released finals reach the server in speech order when an earlier one is retried', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => {
      b.emit({ type: 'final', text: 'A' })
      b.emit({ type: 'final', text: 'B' })
    })
    await flush()
    expect(h.dispatch).not.toHaveBeenCalled()

    // The server rejects the first released final once: the second must not
    // overtake it while it retries.
    h.dispatch.mockImplementationOnce(() => Promise.reject(new h.api.MeetingsApiError('busy', 503)))
    await act(async () => { a.close() })
    await flush(100)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])

    await flush(400) // the retry lands, and only then does B go out
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'A', 'B'])
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('a live final issued while released finals are still in flight queues behind them', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => {
      b.emit({ type: 'final', text: 'A' })
      b.emit({ type: 'final', text: 'B' })
    })
    h.dispatch.mockImplementationOnce(() => Promise.reject(new h.api.MeetingsApiError('busy', 503)))
    await act(async () => { a.close() })
    await flush(100)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])

    // Heard while A is still retrying and B is waiting on it.
    await act(async () => { b.emit({ type: 'final', text: 'C' }) })
    await flush(100)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])

    await flush(400)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'A', 'B', 'C'])
    expect(h.errors()).toEqual([])
  })

  it('a dispatch that never settles stops holding the ones behind it after FLUSH_TIMEOUT_MS', async () => {
    const h = await mount(text => text)
    const ws = await startCapture(h)
    await readyWith(ws)
    const timers = vi.getTimerCount()

    // A wedged backend: the first request goes out and is never answered.
    h.dispatch.mockImplementationOnce(() => new Promise(() => {}))
    await act(async () => { ws.emit({ type: 'final', text: 'A' }) })
    await act(async () => { ws.emit({ type: 'final', text: 'B' }) })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])

    await flush(h.mod.FLUSH_TIMEOUT_MS - 2)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])
    await flush(2)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'B'])
    await flush()
    expect(vi.getTimerCount()).toBe(timers)
    expect(h.hook.result.current.active).toBe(true)
  })

  it('an entry that waited behind another gets its full ordering bound from the moment its own request starts', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => {
      b.emit({ type: 'final', text: 'A' })
      b.emit({ type: 'final', text: 'B' })
      b.emit({ type: 'final', text: 'C' })
    })
    await flush()
    expect(h.dispatch).not.toHaveBeenCalled()

    // A and B each exhaust three rejections before landing on the fourth try:
    // A settles at 4.6 s, B at 9.2 s. C must wait for B, not for a bound that
    // began ticking while B was still queued behind A.
    const busy = () => Promise.reject(new h.api.MeetingsApiError('busy', 503))
    h.dispatch
      .mockImplementationOnce(busy).mockImplementationOnce(busy).mockImplementationOnce(busy)
      .mockImplementationOnce(() => Promise.resolve({ dispatched: 1, text: 'ok' }))
      .mockImplementationOnce(busy).mockImplementationOnce(busy).mockImplementationOnce(busy)
    await act(async () => { a.close() })
    await flush(4_700)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'A', 'A', 'A', 'B'])

    await flush(2_500) // 7.2 s: B is on its third retry; C is still behind it
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'A', 'A', 'A', 'B', 'B', 'B'])

    await flush(2_100) // 9.3 s: B's fourth try lands, then C
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'A', 'A', 'A', 'B', 'B', 'B', 'B', 'C'])
    expect(h.errors()).toEqual([])
  })

  it('flush waits for every queued final, each with its own window, before the caller closes ingress', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 300_000 })

    await act(async () => {
      for (const text of ['A', 'B', 'C', 'D', 'E', 'F']) b.emit({ type: 'final', text })
    })
    expect(h.dispatch).not.toHaveBeenCalled()

    // An ordinary backend: every request answers after 3 s. Serialised, the
    // six take 18 s, past one FLUSH_TIMEOUT_MS and past the deadline that
    // bounds finals arriving DURING the flush; these were queued before it.
    h.dispatch.mockImplementation(() => new Promise(resolve => {
      setTimeout(() => resolve({ dispatched: 1, text: 'ok' }), 3_000)
    }))
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    // The predecessor's drain ends at once: the held finals are released.
    await act(async () => { a.close() })
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])

    await flush(6_700)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'B', 'C'])
    expect(flushed).toBe(false)
    await flush(2_400) // 9.1 s: D goes out
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'B', 'C', 'D'])
    expect(flushed).toBe(false)
    await flush(6_100) // 15.2 s: F, the last one queued, goes out
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'B', 'C', 'D', 'E', 'F'])
    expect(flushed).toBe(false)
    await flush(3_100) // 18.3 s: F has landed
    expect(flushed).toBe(true)
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('flush follows a dispatch queued while it waited, so its retry cannot land on closed ingress', async () => {
    const h = await mount(text => text)
    const ws = await startCapture(h)
    await readyWith(ws)

    // A is out and pending when the user clicks Pause.
    let answerA!: () => void
    h.dispatch.mockImplementationOnce(() => new Promise(resolve => {
      answerA = () => resolve({ dispatched: 1, text: 'ok' })
    }))
    await act(async () => { ws.emit({ type: 'final', text: 'A' }) })
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })

    // Capture keeps running: B queues behind A, and its first request will be
    // refused once, so its retry is what must not meet closed ingress.
    h.dispatch.mockImplementationOnce(() => Promise.reject(new h.api.MeetingsApiError('busy', 503)))
    await act(async () => { ws.emit({ type: 'final', text: 'B' }) })
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A'])

    await act(async () => { answerA() })
    await flush()
    // B went out and was refused: the flush follows it rather than resolving
    // on the tail it saw first.
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'B'])
    expect(flushed).toBe(false)
    await flush(400) // the first retry delay
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['A', 'B', 'B'])
    await flush()
    expect(flushed).toBe(true)
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('flush stops following the dispatch chain at its deadline while capture keeps queueing', async () => {
    const h = await mount(text => text)
    const ws = await startCapture(h)
    await readyWith(ws)
    expect(h.mod.FLUSH_TAIL_DEADLINE_MS).toBe(2 * h.mod.FLUSH_TIMEOUT_MS)

    // An ordinary backend answering after 3 s, and a speaker who does not stop:
    // a new final every 2 s keeps the chain moving for as long as they talk.
    h.dispatch.mockImplementation(() => new Promise(resolve => {
      setTimeout(() => resolve({ dispatched: 1, text: 'ok' }), 3_000)
    }))
    await act(async () => { ws.emit({ type: 'final', text: 'F1' }) })
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    for (let n = 2; n <= 8; n += 1) {
      await flush(2_000)
      expect(flushed).toBe(false)
      await act(async () => { ws.emit({ type: 'final', text: `F${n}` }) })
    }
    // 14 s in: the chain is still moving, but the status change goes out at
    // the deadline, measured from where F1 (the only final queued before the
    // flush) settled at 3 s, rather than when the talking stops.
    await flush(2_000)
    expect(flushed).toBe(false)
    await flush(400)
    expect(flushed).toBe(true)
    expect(h.hook.result.current.active).toBe(true)
  })

  it("flush waits for a draining predecessor's trailing final before the caller closes ingress", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })
    expect(a.kinds()).toContain('stop')

    // Pause lands while the predecessor is still decoding its last utterance.
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    await flush(1_000)
    expect(flushed).toBe(false)
    expect(h.dispatch).not.toHaveBeenCalled()

    // The drain ends a second later: that final is dispatched before the
    // flush resolves, so the status request cannot close ingress ahead of it.
    await act(async () => {
      a.emit({ type: 'final', text: 'tail' })
      a.close()
    })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail'])
    expect(flushed).toBe(true)
    expect(h.hook.result.current.active).toBe(true)
  })

  it('flush keeps waiting for a draining predecessor past FLUSH_TIMEOUT_MS, up to FLUSH_DRAIN_WAIT_MS', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000, final_timeout_ms: 320_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 300_000, final_timeout_ms: 320_000 })
    expect(a.kinds()).toContain('stop')
    expect(h.mod.FLUSH_DRAIN_WAIT_MS).toBe(15_000)

    // A long trailing decode: the server's bound allows it, so a Pause waits
    // past the dispatch bound for it rather than closing ingress under it.
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    await flush(h.mod.FLUSH_TIMEOUT_MS + 1_000)
    expect(flushed).toBe(false)
    expect(h.dispatch).not.toHaveBeenCalled()
    await act(async () => {
      a.emit({ type: 'final', text: 'tail' })
      a.close()
    })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['tail'])
    expect(flushed).toBe(true)
    expect(h.hook.result.current.active).toBe(true)
  })

  it('flush gives up on a draining predecessor that never closes, after FLUSH_DRAIN_WAIT_MS', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    // A long cap on the successor: its own rotation's last resort must not be
    // what ends the predecessor inside the wait.
    await readyWith(b, { max_duration_ms: 300_000 })
    await act(async () => { b.emit({ type: 'final', text: 'held' }) })
    expect(h.dispatch).not.toHaveBeenCalled()

    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    await flush(14_900) // just inside FLUSH_DRAIN_WAIT_MS
    expect(flushed).toBe(false)
    expect(h.dispatch).not.toHaveBeenCalled()
    await flush(200)
    // The bound passed: the held final goes out and the status change proceeds.
    // The predecessor is still left to the server, never closed by the flush.
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['held'])
    expect(flushed).toBe(true)
    expect(a.readyState).toBe(MockSocket.OPEN)
  })

  it("flush gives up on a draining predecessor at the server's own drain bound when that is shorter", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000, final_timeout_ms: 9_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 300_000, final_timeout_ms: 9_000 })
    await act(async () => { b.emit({ type: 'final', text: 'held' }) })
    expect(h.dispatch).not.toHaveBeenCalled()

    // The server will not deliver anything after its own 9 s: waiting the full
    // 15 s would hold the Pause for nothing.
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    await flush(8_900)
    expect(flushed).toBe(false)
    expect(h.dispatch).not.toHaveBeenCalled()
    await flush(200)
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['held'])
    expect(flushed).toBe(true)
    expect(a.readyState).toBe(MockSocket.OPEN)
  })

  it("the successor's stall watchdog waits out a predecessor's drain, then gets its normal window back", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000, final_timeout_ms: 50_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 300_000 })
    expect(a.kinds()).toContain('stop')
    expect(sockets).toHaveLength(2)

    // The predecessor's trailing final takes a long decode; the server's lock
    // keeps the successor silent for longer than the stall window.
    await flush(26_000)
    expect(sockets).toHaveLength(2)
    expect(h.hook.result.current.active).toBe(true)

    // The drain ends: the successor gets a fresh, normal stall window.
    await act(async () => { a.close() })
    await flush(16_000)
    expect(sockets).toHaveLength(2)
    await flush(10_000)
    expect(sockets).toHaveLength(3)
    // The reconnect reports itself live again only once its own `ready` lands.
    expect(h.hook.result.current.connecting).toBe(true)
    await act(async () => { lastSocket().becomeReady() })
    expect(h.hook.result.current.active).toBe(true)
  })

  it('a rotation that finds the predecessor still draining waits for it instead of closing it', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    // A 120 s cap with a 40 s drain bound: the lead is 45 s, so a rotation
    // comes due 75 s into a lease and the last resort is 30 s before its cap.
    await readyWith(a, { max_duration_ms: 120_000, final_timeout_ms: 40_000 })
    await keepAlive(a, 75_000)
    const b = lastSocket()
    expect(b).not.toBe(a)
    await readyWith(b, { max_duration_ms: 120_000, final_timeout_ms: 40_000 })
    expect(a.kinds()).toContain('stop')

    // b's own rotation comes due while a is still decoding: a is left open,
    // because the server would still deliver the final it is working on.
    await keepAlive(b, 75_000)
    expect(sockets).toHaveLength(2)
    expect(a.readyState).not.toBe(MockSocket.CLOSED)

    // a finishes: the next retry, a few seconds later, opens the successor.
    await act(async () => { a.close() })
    await keepAlive(b, 5_000)
    expect(sockets).toHaveLength(3)
    expect(h.hook.result.current.active).toBe(true)
    expect(h.errors()).toEqual([])
  })

  it('closes a predecessor still draining once the live lease is within the lead of its own cap', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 120_000, final_timeout_ms: 40_000 })
    await keepAlive(a, 75_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 120_000, final_timeout_ms: 40_000 })

    // b went live at 75 s: its cap is at 195 s and the last resort at 165 s.
    await keepAlive(b, 89_000)
    expect(sockets).toHaveLength(2)
    expect(a.readyState).not.toBe(MockSocket.CLOSED)
    await keepAlive(b, 2_000)
    expect(a.readyState).toBe(MockSocket.CLOSED)
    expect(sockets).toHaveLength(3)
    expect(h.hook.result.current.active).toBe(true)
  })

  it("a draining predecessor's decode failure is reported, so the utterance it was finishing is not lost silently", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => {
      a.emit({ type: 'error', code: 'stt_decode_failed', message: 'decode failed' })
    })
    expect(h.errors()).toEqual(['decode failed'])
    // The live lease carries on; the predecessor's close still releases the hold.
    expect(h.hook.result.current.active).toBe(true)
    await act(async () => { b.emit({ type: 'final', text: 'after' }) })
    await act(async () => { a.close() })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['after'])
  })

  it("a draining predecessor told it hit the cap stays silent, as do a dropped successor and a replaced lease", async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => {
      a.emit({ type: 'error', code: 'stt_max_duration_exceeded', message: 'cap' })
    })
    expect(h.errors()).toEqual([])
    await act(async () => { a.close() })
    await flush()
    // A lease already replaced and closed has nothing to say either.
    await act(async () => { a.emit({ type: 'error', code: 'stt_decode_failed', message: 'late' }) })
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('leaves the predecessor in charge when the successor fails to open', async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    MockSocket.failNextOpen = true
    await flush(10_000)
    expect(sockets).toHaveLength(2)

    await act(async () => { lastNode().speak(100) })
    expect(a.audioBytes()).toEqual([100])
    expect(a.kinds()).not.toContain('stop')
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('retries a successor that failed to open and takes over on the retry', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 60_000 })
    // The third STT slot is taken (a dictation, a second tab): the open fails.
    MockSocket.failNextOpen = true
    await keepAlive(a, 30_000)
    expect(sockets).toHaveLength(2)
    expect(h.hook.result.current.active).toBe(true)

    // A short backoff later the rotation tries again, while the cap is still
    // far enough away for a successor to connect.
    await keepAlive(a, 5_000)
    expect(sockets).toHaveLength(3)
    const c = lastSocket()
    await readyWith(c, { max_duration_ms: 60_000 })
    expect(a.kinds()).toContain('stop')
    await act(async () => { lastNode().speak(7) })
    expect(c.audioBytes()).toEqual([7])
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('stops retrying a successor once less than the connect allowance remains before the cap', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    // Every attempt fails: the first at 10 s, then one every 5 s, the last at
    // 35 s, when exactly the connect allowance remains.
    MockSocket.failNextOpen = true
    await keepAlive(a, 10_000)
    for (let attempt = 0; attempt < 5; attempt += 1) {
      MockSocket.failNextOpen = true
      await keepAlive(a, 5_000)
    }
    expect(sockets).toHaveLength(7)
    await keepAlive(a, 5_000)
    expect(sockets).toHaveLength(7)
    expect(h.hook.result.current.active).toBe(true)
  })

  it('a user stop while the successor is still connecting drops it and releases the microphone', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    expect(b).not.toBe(a)

    // The user stops inside the successor's open-to-ready window.
    await act(async () => { h.hook.result.current.stop() })
    // The server's `ready` for the successor lands AFTER the stop: it must not
    // take over the audio, and the predecessor is stopped exactly once.
    await readyWith(b, { max_duration_ms: 40_000 })
    await act(async () => { lastNode().speak(123) })
    expect(b.audioBytes()).toEqual([])
    expect(a.kinds().filter(k => k === 'stop')).toHaveLength(1)

    await act(async () => { b.emit({ type: 'final', text: 'said after stop' }) })
    await flush(8_000) // CLOSE_GRACE_MS
    expect(h.dispatch).not.toHaveBeenCalled()
    expect(a.readyState).toBe(MockSocket.CLOSED)
    expect(b.readyState).toBe(MockSocket.CLOSED)
    expect(stoppedTracks).toEqual(['mic'])
    expect(h.hook.result.current.active).toBe(false)
    expect(h.errors()).toEqual([])
    // Nothing left ticking: the dropped successor armed no rotation timer.
    expect(vi.getTimerCount()).toBe(0)
  })

  it("a successor's connection error after teardown is not reported", async () => {
    const h = await mount()
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    expect(b).not.toBe(a)

    // A real socket that is closed while CONNECTING fires `error` then `close`
    // on a later task; the double delivers neither, so hand them over by hand.
    await act(async () => { h.hook.result.current.stop() })
    await flush(8_000)
    await act(async () => { b.onerror?.(); b.onclose?.() })
    expect(h.errors()).toEqual([])
    expect(h.hook.result.current.active).toBe(false)
  })

  it('a user stop during a drain releases the microphone but lets the predecessor finish', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    expect(b).not.toBe(a)
    await readyWith(b, { max_duration_ms: 40_000 })

    await act(async () => { h.hook.result.current.stop() })
    expect(b.kinds()).toContain('stop')
    await flush(8_000) // CLOSE_GRACE_MS
    expect(b.readyState).toBe(MockSocket.CLOSED)
    expect(stoppedTracks).toEqual(['mic'])
    expect(h.hook.result.current.active).toBe(false)
    // The predecessor is still decoding the utterance the rotation stopped it
    // in: it stays open, and its final still reaches the transcript.
    expect(a.readyState).toBe(MockSocket.OPEN)
    await act(async () => { a.emit({ type: 'final', text: 'finished after the stop' }) })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['finished after the stop'])
    await act(async () => { a.close() })
    await flush()
    expect(h.errors()).toEqual([])
  })

  it('held finals are released with the time they arrived, not the time of the release', async () => {
    const h = await mount(text => text)
    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })

    // A slow drain: the successor hears the same word twice, 7 s apart, and
    // both wait behind the predecessor's trailing final.
    const heardAt = new Date('2026-01-01T00:00:00Z').getTime()
    vi.setSystemTime(heardAt)
    await act(async () => { b.emit({ type: 'final', text: 'yes' }) })
    await flush(7_000)
    await act(async () => { b.emit({ type: 'final', text: 'yes' }) })
    expect(h.onFinal).not.toHaveBeenCalled()

    await act(async () => { a.close() })
    await flush()
    // Released in one breath, but each carries its own arrival, so the dedup
    // sees two utterances 7 s apart rather than one repeated at the release.
    expect(h.onFinal.mock.calls).toEqual([['yes', heardAt], ['yes', heardAt + 7_000]])
  })

  it('flush releases held finals and resolves after their dispatches settle', async () => {
    const h = await mount(text => text)
    // Nothing captured and nothing held: it resolves at once.
    await act(async () => { await h.hook.result.current.flush() })

    const a = await startCapture(h)
    await readyWith(a, { max_duration_ms: 40_000 })
    await flush(10_000)
    const b = lastSocket()
    await readyWith(b, { max_duration_ms: 40_000 })
    await act(async () => {
      b.emit({ type: 'final', text: 'held one' })
      b.emit({ type: 'final', text: 'held two' })
    })
    expect(h.dispatch).not.toHaveBeenCalled()

    const settle: Array<() => void> = []
    h.dispatch.mockImplementation(() => new Promise(resolve => {
      settle.push(() => resolve({ dispatched: 1, text: 'ok' }))
    }))
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    // Released once the predecessor's drain ends; the second request leaves
    // only once the first has settled, so they reach the server in order.
    await act(async () => { a.close() })
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['held one'])
    await flush()
    expect(flushed).toBe(false)

    await act(async () => { settle[0]() })
    await flush()
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['held one', 'held two'])
    expect(flushed).toBe(false)
    await act(async () => { settle[1]() })
    await flush()
    expect(flushed).toBe(true)

    // A flush is not a stop: the microphone and the live lease stay up.
    expect(h.hook.result.current.active).toBe(true)
    expect(stoppedTracks).toEqual([])
    expect(b.kinds()).not.toContain('stop')
    await act(async () => { lastNode().speak(100) })
    expect(b.audioBytes()).toEqual([100])
    expect(h.errors()).toEqual([])
  })

  it('flush gives up waiting after FLUSH_TIMEOUT_MS when a dispatch never settles', async () => {
    const h = await mount(text => text)
    const ws = await startCapture(h)
    await readyWith(ws)
    const timers = vi.getTimerCount()

    // A dispatch that settles ends the wait at once, and takes the bound with it.
    let answer!: () => void
    h.dispatch.mockImplementationOnce(() => new Promise(resolve => {
      answer = () => resolve({ dispatched: 1, text: 'ok' })
    }))
    await act(async () => { ws.emit({ type: 'final', text: 'answered' }) })
    let flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    expect(flushed).toBe(false)
    await act(async () => { answer() })
    await flush(0)
    expect(flushed).toBe(true)
    expect(vi.getTimerCount()).toBe(timers)

    // A wedged backend: the request goes out and is never answered.
    h.dispatch.mockImplementation(() => new Promise(() => {}))
    await act(async () => { ws.emit({ type: 'final', text: 'unanswered' }) })
    expect(h.dispatch.mock.calls.map(c => c[1])).toEqual(['answered', 'unanswered'])
    flushed = false
    await act(async () => { void h.hook.result.current.flush().then(() => { flushed = true }) })
    await flush(h.mod.FLUSH_TIMEOUT_MS - 1)
    expect(flushed).toBe(false)
    await flush(1)
    expect(flushed).toBe(true)
    expect(vi.getTimerCount()).toBe(timers)

    // The caller proceeds; capture does not stop for it.
    expect(h.hook.result.current.active).toBe(true)
    expect(stoppedTracks).toEqual([])
    expect(ws.readyState).toBe(MockSocket.OPEN)
    expect(ws.kinds()).not.toContain('stop')
    await act(async () => { lastNode().speak(100) })
    expect(ws.audioBytes()).toEqual([100])
    expect(h.errors()).toEqual([])
  })
})
