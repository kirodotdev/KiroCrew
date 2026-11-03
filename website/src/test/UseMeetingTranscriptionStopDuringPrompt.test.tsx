/**
 * A stop or an unmount that lands while the microphone permission prompt is
 * open must leave nothing running once the prompt is answered.
 *
 * `cleanup()` runs at the stop, before `getUserMedia` resolves, so it has no
 * stream to release and no socket to close. Without a re-check after that
 * await, the late stream is kept and a socket is opened for a session nobody
 * owns: the microphone stays captured and the STT socket stays up.
 *
 * Same doubles as `UseMeetingTranscriptionCoverage.test.tsx`.
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

/** A getUserMedia whose permission prompt stays open until the test answers it. */
function heldPrompt() {
  let answer: (s: MediaStream) => void = () => {}
  let deny: (e: unknown) => void = () => {}
  getUserMedia.mockImplementationOnce(() => new Promise<MediaStream>((resolve, reject) => {
    answer = resolve
    deny = reject
  }))
  return { allow: (s: MediaStream) => answer(s), deny: (e: unknown) => deny(e) }
}

describe('useMeetingTranscription — stop while the permission prompt is open', () => {
  it('releases the late stream and opens no socket after a stop', async () => {
    const h = await mount()
    const prompt = heldPrompt()
    await act(async () => { void h.hook.result.current.start() })
    await act(async () => { h.hook.result.current.stop() })

    prompt.allow(makeStream('late'))
    await flush()

    expect(sockets).toHaveLength(0)
    expect(stoppedTracks).toEqual(['late'])
    expect(h.hook.result.current.active).toBe(false)
    expect(h.hook.result.current.connecting).toBe(false)
  })

  it('releases the late stream and opens no socket after an unmount', async () => {
    const h = await mount()
    const prompt = heldPrompt()
    await act(async () => { void h.hook.result.current.start() })
    h.hook.unmount()

    prompt.allow(makeStream('late'))
    await flush()

    expect(sockets).toHaveLength(0)
    expect(stoppedTracks).toEqual(['late'])
  })

  it('reports no microphone error for a prompt denied after the stop', async () => {
    const h = await mount()
    const prompt = heldPrompt()
    await act(async () => { void h.hook.result.current.start() })
    await act(async () => { h.hook.result.current.stop() })

    prompt.deny(new DOMException('denied', 'NotAllowedError'))
    await flush()

    expect(h.errors()).toEqual([])
  })

  it('lets a start after the stop own the session', async () => {
    const h = await mount()
    const prompt = heldPrompt()
    await act(async () => { void h.hook.result.current.start() })
    await act(async () => { h.hook.result.current.stop() })
    await act(async () => { void h.hook.result.current.start() })
    await flush()
    const ws = lastSocket()
    await act(async () => { ws.becomeReady() })

    prompt.allow(makeStream('late'))
    await flush()

    expect(sockets).toHaveLength(1)
    expect(stoppedTracks).toEqual(['late'])
    expect(h.hook.result.current.active).toBe(true)
  })

  it('reports a start pending on the prompt, which no published flag shows', async () => {
    const h = await mount()
    heldPrompt()
    expect(h.hook.result.current.isStarting()).toBe(false)
    await act(async () => { void h.hook.result.current.start() })

    expect(h.hook.result.current.isStarting()).toBe(true)
    expect(h.hook.result.current.active).toBe(false)
    expect(h.hook.result.current.connecting).toBe(false)

    await act(async () => { h.hook.result.current.stop() })
    expect(h.hook.result.current.isStarting()).toBe(false)
  })

  it('still starts normally when nothing stops it during the prompt', async () => {
    const h = await mount()
    const prompt = heldPrompt()
    await act(async () => { void h.hook.result.current.start() })
    prompt.allow(makeStream('mic'))
    await flush()
    await act(async () => { lastSocket().becomeReady() })

    expect(sockets).toHaveLength(1)
    expect(stoppedTracks).toEqual([])
    expect(h.hook.result.current.active).toBe(true)
  })
})
