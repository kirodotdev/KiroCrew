/**
 * A meeting socket that fails before `ready` must not be reopened, and its
 * failure reported, about once a second.
 *
 * The session effect reopens transcription whenever the hook is neither active
 * nor connecting. A setup refusal that a retry cannot fix halts the hook,
 * and a retryable one is reopened after a growing wait and reported once until
 * a socket goes live or the user stops.
 *
 * Same doubles as `UseMeetingTranscriptionCoverage.test.tsx`, cut down to what
 * these tests drive.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import {
  REOPEN_BACKOFF_BASE_MS as BASE,
  REOPEN_BACKOFF_MAX_MS as MAX,
  REOPEN_BACKOFF_SOON_MAX_MS as SOON,
} from './hooks/useMeetingTranscription'

type HookModule = typeof import('./hooks/useMeetingTranscription')

const sockets: MockSocket[] = []
const lastSocket = () => sockets[sockets.length - 1]

class MockSocket {
  static readonly OPEN = 1
  static readonly CLOSED = 3
  /** Set by a test to make the NEXT socket report a failure to open. */
  static failNextOpen = false

  readyState = 1
  binaryType = ''
  bufferedAmount = 0
  onopen: (() => void) | null = null
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor() {
    sockets.push(this)
    const fail = MockSocket.failNextOpen
    MockSocket.failNextOpen = false
    // A real socket opens on a later task, never inside the constructor.
    setTimeout(() => { if (fail) this.onerror?.(); else this.onopen?.() }, 0)
  }

  send() {}

  close() {
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    const fire = this.onclose
    this.onclose = null
    fire?.()
  }

  emit(msg: unknown) { this.onmessage?.({ data: JSON.stringify(msg) }) }
}

class MockWorkletNode {
  port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null }
  connect() {}
  disconnect() {}
}

class MockAudioContext {
  audioWorklet = { addModule: () => Promise.resolve() }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { return Promise.resolve() }
}

function makeStream() {
  const track = { stop: () => {}, readyState: 'live', getSettings: () => ({ deviceId: 'dev-1' }) }
  return { getAudioTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream
}

let getUserMedia: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetModules()
  sockets.length = 0
  MockSocket.failNextOpen = false
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

async function flush(ms = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

async function mount() {
  const mod: HookModule = await import('./hooks/useMeetingTranscription')
  const onError = vi.fn()
  const hook = renderHook(() =>
    mod.useMeetingTranscription({ meetingId: 'meet-1', onCaption: vi.fn(), onError }),
  )
  return {
    mod,
    hook,
    start: async () => { await act(async () => { void hook.result.current.start() }) },
    errors: () => onError.mock.calls.map(c => String(c[0])),
  }
}

/** The server refuses the socket before `ready` with *code*, then closes it. */
async function refuse(code: string) {
  const ws = lastSocket()
  await act(async () => { ws.emit({ type: 'error', code }) })
  await act(async () => { ws.close() })
}

describe('useMeetingTranscription — reopening after a socket fails before ready', () => {
  it('keeps the documented 1 s base, 5 s soon cap and 30 s cap', () => {
    expect([BASE, SOON, MAX]).toEqual([1_000, 5_000, 30_000])
  })

  it('waits 1 s, doubling per failure up to 30 s, and not at all before the first open', async () => {
    const { reopenDelayMs } = await import('./hooks/useMeetingTranscription')
    expect([0, 1, 2, 3, 6, 7, 50].map(n => reopenDelayMs(n))).toEqual([0, BASE, 2 * BASE, 4 * BASE, MAX, MAX, MAX])
  })

  it('caps the wait at 5 s for a refusal that clears once the model is ready', async () => {
    const { reopenDelayMs } = await import('./hooks/useMeetingTranscription')
    for (const code of ['stt_model_missing', 'stt_import_failed']) {
      expect([1, 3, 4, 6, 50].map(n => reopenDelayMs(n, code))).toEqual([BASE, 4 * BASE, SOON, SOON, SOON])
    }
    expect(reopenDelayMs(6, 'stt_session_failed')).toBe(MAX)
  })

  it('a model still downloading is retried every 5 s once the wait reaches its cap', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_model_missing')
    // Waits of 1, 2 and 4 s, then the 5 s cap.
    for (const wait of [BASE, 2 * BASE, 4 * BASE, SOON, SOON]) {
      const before = sockets.length
      await h.start()
      await flush(wait - 1)
      expect(sockets).toHaveLength(before)
      await flush(2)
      expect(sockets).toHaveLength(before + 1)
      await refuse('stt_model_missing')
    }
    expect(h.errors()).toEqual(['stt_model_missing'])
  })

  it('a failure while the user is pausing is reported and does not delay or hide the resume', async () => {
    const h = await mount()
    await h.start()
    await flush()
    // Pause while the socket is still connecting: stop() sends `stop` and
    // waits for the server's close, which the refusal lands inside.
    await act(async () => { h.hook.result.current.stop() })
    await refuse('stt_model_missing')
    expect(h.errors()).toEqual(['stt_model_missing'])

    // Resume: the socket opens at once and the same refusal is reported again.
    await h.start()
    await flush()
    expect(sockets).toHaveLength(2)
    await refuse('stt_model_missing')
    expect(h.errors()).toEqual(['stt_model_missing', 'stt_model_missing'])
  })

  it.each([
    'stt_no_wheel_for_platform',
    'stt_unsupported_cpu',
    'stt_load_crashed',
    'stt_native_probe_crashed',
    'stt_apple_unsupported',
    'stt_apple_needs_toolchain',
    'stt_codex_missing',
    'stt_codex_login_required',
  ])('halts on %s, which no retry can fix, and reports it once', async code => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse(code)
    expect(h.hook.result.current.halted).toBe(true)

    await h.start()
    await flush(2 * MAX)
    expect(sockets).toHaveLength(1)
    expect(h.errors()).toEqual([code])
  })

  it.each(['stt_model_missing', 'stt_import_failed', 'stt_codex_voice_unavailable'])(
    'reopens %s, which a later attempt can fix, after a growing wait and reports it once',
    async code => {
      const h = await mount()
      await h.start()
      await flush()
      await refuse(code)
      expect(h.hook.result.current.halted).toBe(false)

      await h.start()
      await flush(BASE - 1)
      expect(sockets).toHaveLength(1)
      await flush(2)
      expect(sockets).toHaveLength(2)
      await refuse(code)
      expect(h.errors()).toEqual([code])
    },
  )

  it('waits before the next attempt after a socket that failed to open', async () => {
    const h = await mount()
    MockSocket.failNextOpen = true
    await h.start()
    await flush()
    expect(h.errors()).toEqual(['connection', 'disconnected'])

    MockSocket.failNextOpen = true
    await h.start()
    await flush(BASE - 1)
    expect(sockets).toHaveLength(1)
    await flush(2)
    expect(sockets).toHaveLength(2)
    // The same failure again is not reported again.
    expect(h.errors()).toEqual(['connection', 'disconnected'])

    // Two failures in a row: the next attempt waits 2 s.
    await h.start()
    await flush(2 * BASE - 1)
    expect(sockets).toHaveLength(2)
    await flush(2)
    expect(sockets).toHaveLength(3)
  })

  it('reopens a retryable refusal after a growing wait and reports it once', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_model_missing')
    expect(h.hook.result.current.halted).toBe(false)

    // First reopen: nothing at once, a socket after 1 s.
    await h.start()
    await flush(BASE - 1)
    expect(sockets).toHaveLength(1)
    await flush(2)
    expect(sockets).toHaveLength(2)
    await refuse('stt_model_missing')

    // Second reopen waits 2 s.
    await h.start()
    await flush(2 * BASE - 1)
    expect(sockets).toHaveLength(2)
    await flush(2)
    expect(sockets).toHaveLength(3)
    await refuse('stt_model_missing')

    expect(h.errors()).toEqual(['stt_model_missing'])
  })

  it('reports a different reason on a later attempt', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_model_missing')
    await h.start()
    await flush(BASE + 1)
    await refuse('stt_session_failed')

    expect(h.errors()).toEqual(['stt_model_missing', 'stt_session_failed'])
  })

  it('reports a code again when it returns after a different one', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_model_missing')
    await h.start()
    await flush(BASE + 1)
    await refuse('stt_session_failed')
    await h.start()
    await flush(2 * BASE + 1)
    await refuse('stt_model_missing')
    await h.start()
    await flush(4 * BASE + 1)
    await refuse('stt_model_missing')

    expect(sockets).toHaveLength(4)
    expect(h.errors()).toEqual(['stt_model_missing', 'stt_session_failed', 'stt_model_missing'])
  })

  it('an unmount during the wait cancels it and opens nothing', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_model_missing')
    const idle = vi.getTimerCount()

    await h.start()
    expect(vi.getTimerCount()).toBe(idle + 1)
    h.hook.unmount()
    expect(vi.getTimerCount()).toBe(idle)
    await flush(SOON)
    expect(sockets).toHaveLength(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('opens at once, and reports again, after a socket went live', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_session_failed')
    await h.start()
    await flush(BASE + 1)
    await act(async () => { lastSocket().emit({ type: 'ready' }) })
    expect(h.hook.result.current.active).toBe(true)
    await refuse('stt_session_failed')

    await h.start()
    await flush()
    expect(sockets).toHaveLength(3)
    await refuse('stt_session_failed')
    expect(h.errors()).toEqual(['stt_session_failed', 'stt_session_failed', 'stt_session_failed'])
  })

  it('a stop during the wait opens nothing, and the next start opens at once', async () => {
    const h = await mount()
    await h.start()
    await flush()
    await refuse('stt_model_missing')
    expect(getUserMedia).toHaveBeenCalledTimes(1)

    await h.start()
    expect(h.hook.result.current.isStarting()).toBe(true)
    await act(async () => { h.hook.result.current.stop() })
    await flush(SOON)
    expect(sockets).toHaveLength(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)

    // stop() (pause or end) ends the failure run: resuming opens at once and
    // says again why it fails.
    await h.start()
    await flush()
    expect(sockets).toHaveLength(2)
    await refuse('stt_model_missing')
    expect(h.errors()).toEqual(['stt_model_missing', 'stt_model_missing'])
  })
})
