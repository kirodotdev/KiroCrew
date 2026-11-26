/**
 * A streaming capture whose track the browser stops feeding ends with a reason.
 *
 * WebKit can mute or end a capture track with no error at all. Before this, the
 * hold bar kept reading "holding" over a flat level until the page was reloaded.
 * Also pinned here: a start that finds a previous session's socket still set
 * starts fresh instead of returning false with nothing on screen, and a cancel
 * during a stalled `resume()` does not report a start failure of its own.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { TRACK_MUTE_GRACE_MS } from '../hooks/mic'

const MUTED = 'hooks.mic.browser_muted_the_microphone'
const ENDED = 'hooks.mic.microphone_disconnected_by_browser'

vi.mock('../hooks/mic', async importOriginal => ({
  ...(await importOriginal<typeof import('../hooks/mic')>()),
  acquireMicStream: () => Promise.resolve(makeStream()),
  createLevelMeter: () => () => {},
  activeDeviceId: () => 'dev-1',
}))

class FakeTrack extends EventTarget {
  muted = false
  enabled = true
  readyState = 'live'
  label = 'Mock Mic'
  stop = vi.fn(() => { this.readyState = 'ended' })
  getSettings() { return { deviceId: 'dev-1' } }
  private watched = new Set<string>()
  addEventListener(type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | AddEventListenerOptions) {
    this.watched.add(type)
    super.addEventListener(type, listener, options)
  }
  removeEventListener(type: string, listener: EventListenerOrEventListenerObject | null, options?: boolean | EventListenerOptions) {
    this.watched.delete(type)
    super.removeEventListener(type, listener, options)
  }
  listenersLeft() { return this.watched.size }
}

const tracks: FakeTrack[] = []

function makeStream() {
  const track = new FakeTrack()
  tracks.push(track)
  return { getAudioTracks: () => [track], getTracks: () => [track] }
}

const sockets: MockSocket[] = []

class MockSocket {
  constructor() { sockets.push(this) }
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 3
  readyState = 1
  binaryType = ''
  onmessage: ((e: { data: unknown }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  send() {}
  close() {
    if (this.readyState === MockSocket.CLOSED) return
    this.readyState = MockSocket.CLOSED
    const fire = this.onclose
    this.onclose = null
    fire?.()
  }
}

class MockWorkletNode {
  port: { onmessage: ((e: { data: ArrayBuffer }) => void) | null } = { onmessage: null }
  connect() {}
  disconnect() {}
}

const audio = {
  state: 'running' as AudioContextState,
  resume: (): Promise<void> => Promise.resolve(),
}

class MockAudioContext {
  audioWorklet = { addModule: () => Promise.resolve() }
  get state() { return audio.state }
  resume() { return audio.resume() }
  createMediaStreamSource() { return { connect() {}, disconnect() {} } }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  vi.useFakeTimers()
  tracks.length = 0
  sockets.length = 0
  audio.state = 'running'
  audio.resume = () => Promise.resolve()
  vi.stubGlobal('WebSocket', MockSocket as unknown as typeof WebSocket)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.stubGlobal('AudioWorkletNode', MockWorkletNode as unknown as typeof AudioWorkletNode)
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function startCapture() {
  const { useStreamingStt } = await import('../hooks/useStreamingStt')
  const onError = vi.fn()
  const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn(), onError }))
  let started: boolean | undefined
  await act(async () => { started = await hook.result.current.start() })
  return { hook, onError, started, track: tracks.at(-1)! }
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

function mute(track: FakeTrack) {
  act(() => { track.muted = true; track.dispatchEvent(new Event('mute')) })
}

describe('streaming capture track health', () => {
  it('stops with the muted message when the track stays muted past the grace period', async () => {
    const { hook, onError, started, track } = await startCapture()
    expect(started).toBe(true)
    expect(hook.result.current.recording).toBe(true)

    mute(track)
    await advance(TRACK_MUTE_GRACE_MS)

    expect(onError).toHaveBeenCalledWith(i18nT(MUTED))
    expect(hook.result.current.recording).toBe(false)
    expect(track.stop).toHaveBeenCalled()
  })

  it('keeps recording through a brief mute', async () => {
    const { hook, onError, track } = await startCapture()

    mute(track)
    await advance(TRACK_MUTE_GRACE_MS / 2)
    act(() => { track.muted = false; track.dispatchEvent(new Event('unmute')) })
    await advance(TRACK_MUTE_GRACE_MS * 2)

    expect(onError).not.toHaveBeenCalled()
    expect(hook.result.current.recording).toBe(true)
  })

  it('stops with the disconnected message when the browser ends the track', async () => {
    const { hook, onError, track } = await startCapture()

    act(() => { track.dispatchEvent(new Event('ended')) })

    expect(onError).toHaveBeenCalledWith(i18nT(ENDED))
    expect(hook.result.current.recording).toBe(false)
  })

  it('says nothing for a late event on the track of a stopped capture', async () => {
    const { hook, onError, track } = await startCapture()

    act(() => { hook.result.current.stop() })
    mute(track)
    act(() => { track.dispatchEvent(new Event('ended')) })
    await advance(TRACK_MUTE_GRACE_MS * 2)

    expect(onError).not.toHaveBeenCalled()
  })

  it('detaches the watch on teardown', async () => {
    const { hook, onError, track } = await startCapture()
    expect(track.listenersLeft()).toBeGreaterThan(0)

    act(() => { hook.result.current.cancel() })
    expect(track.listenersLeft()).toBe(0)
    mute(track)
    await advance(TRACK_MUTE_GRACE_MS * 2)

    expect(onError).not.toHaveBeenCalled()
  })
})

describe('streaming start with a previous session still set', () => {
  it('tears the old session down and starts fresh instead of refusing silently', async () => {
    const { hook, onError, track: first } = await startCapture()
    expect(sockets).toHaveLength(1)

    let again: boolean | undefined
    await act(async () => { again = await hook.result.current.start() })

    expect(again).toBe(true)
    expect(sockets).toHaveLength(2)
    expect(sockets[0].readyState).toBe(MockSocket.CLOSED)
    expect(first.stop).toHaveBeenCalled()
    expect(hook.result.current.recording).toBe(true)
    expect(onError).not.toHaveBeenCalled()
  })
})

describe('cancel during a stalled resume', () => {
  it('does not report a start failure for the startup it cancelled', async () => {
    const { useStreamingStt } = await import('../hooks/useStreamingStt')
    let rejectResume: (e: Error) => void = () => {}
    audio.state = 'suspended'
    audio.resume = () => new Promise((_, reject) => { rejectResume = reject })
    const onError = vi.fn()
    const hook = renderHook(() => useStreamingStt({ onPartial: vi.fn(), onFinal: vi.fn(), onError }))

    let pending: Promise<boolean> = Promise.resolve(false)
    act(() => { pending = hook.result.current.start() })
    await advance(0)
    act(() => { hook.result.current.cancel() })
    // Closing the context is what rejects a pending resume in a real browser.
    rejectResume(new DOMException('closed', 'InvalidStateError'))

    await act(async () => { expect(await pending).toBe(false) })
    expect(onError).not.toHaveBeenCalled()
  })
})
