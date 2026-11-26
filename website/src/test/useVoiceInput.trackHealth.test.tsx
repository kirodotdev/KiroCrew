/**
 * A batch recording whose track the browser stops feeding ends with a reason.
 *
 * The batch recorder shares the track watch with the streaming path: a track
 * that stays muted past the grace period, or that the browser ends, stops the
 * recording and shows a message instead of a "recording" state over silence.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { TRACK_MUTE_GRACE_MS } from '../hooks/mic'

const MUTED = 'hooks.mic.browser_muted_the_microphone'
const ENDED = 'hooks.mic.microphone_disconnected_by_browser'

vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, start: vi.fn(), stop: vi.fn(), cancel: vi.fn() }),
}))

const sttTranscribe = vi.fn().mockResolvedValue({ text: 'hello' })
vi.mock('../api/client', () => ({ api: { sttTranscribe: (...a: unknown[]) => sttTranscribe(...a) } }))

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

const recorders: MockMediaRecorder[] = []
class MockMediaRecorder {
  static isTypeSupported() { return true }
  state: 'inactive' | 'recording' = 'inactive'
  stream: unknown
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(s: unknown) { this.stream = s; recorders.push(this) }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive'; this.onstop?.() }
  feed() { this.ondataavailable?.({ data: new Blob(['x'.repeat(200)]) }) }
}

class MockAudioContext {
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() { return { fftSize: 0, frequencyBinCount: 16, getByteTimeDomainData() {}, connect() {} } }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  vi.useFakeTimers()
  tracks.length = 0
  recorders.length = 0
  sttTranscribe.mockClear()
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(() => Promise.resolve(makeStream())), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
  vi.stubGlobal('MediaRecorder', MockMediaRecorder as unknown as typeof MediaRecorder)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  vi.resetModules()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function startRecording() {
  const { useVoiceInput } = await import('../hooks/useVoiceInput')
  const hook = renderHook(() => useVoiceInput(vi.fn()))
  await act(async () => { await hook.result.current.start() })
  expect(hook.result.current.recording).toBe(true)
  return { hook, track: tracks.at(-1)! }
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

function mute(track: FakeTrack) {
  act(() => { track.muted = true; track.dispatchEvent(new Event('mute')) })
}

describe('batch capture track health', () => {
  it('stops with the muted message when the track stays muted past the grace period', async () => {
    const { hook, track } = await startRecording()

    mute(track)
    await advance(TRACK_MUTE_GRACE_MS)

    expect(hook.result.current.error).toBe(i18nT(MUTED))
    expect(hook.result.current.recording).toBe(false)
    expect(track.stop).toHaveBeenCalled()
  })

  it('keeps recording through a brief mute', async () => {
    const { hook, track } = await startRecording()

    mute(track)
    await advance(TRACK_MUTE_GRACE_MS / 2)
    act(() => { track.muted = false; track.dispatchEvent(new Event('unmute')) })
    await advance(TRACK_MUTE_GRACE_MS * 2)

    expect(hook.result.current.error).toBeNull()
    expect(hook.result.current.recording).toBe(true)
  })

  it('stops with the disconnected message when the browser ends the track, keeping what was said', async () => {
    const { hook, track } = await startRecording()
    act(() => { recorders.at(-1)!.feed() })

    act(() => { track.dispatchEvent(new Event('ended')) })
    await advance(0)

    expect(hook.result.current.error).toBe(i18nT(ENDED))
    expect(hook.result.current.recording).toBe(false)
    expect(sttTranscribe).toHaveBeenCalledTimes(1)
  })

  it('detaches the watch when the recording stops, so a late event says nothing', async () => {
    const { hook, track } = await startRecording()

    act(() => { hook.result.current.stop() })
    expect(track.listenersLeft()).toBe(0)
    mute(track)
    act(() => { track.dispatchEvent(new Event('ended')) })
    await advance(TRACK_MUTE_GRACE_MS * 2)

    expect(hook.result.current.error).toBeNull()
  })

  it('detaches the watch on unmount', async () => {
    const { hook, track } = await startRecording()
    expect(track.listenersLeft()).toBeGreaterThan(0)

    hook.unmount()

    expect(track.listenersLeft()).toBe(0)
  })
})
