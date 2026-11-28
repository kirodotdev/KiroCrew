/**
 * A voice startup that never reaches capture ends with an error instead of
 * holding the start latch forever.
 *
 * A `getUserMedia`, worklet load or `AudioContext.resume()` that never settles
 * (seen on iOS WebKit) used to leave `start()` pending with no message, so the
 * hold bar read "holding" over silence and every later press was swallowed by
 * the latch until the page was reloaded.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { i18nT } from '../i18n/t'

const TIMED_OUT = 'hooks.useVoiceInput.microphone_start_timed_out'

const stream = {
  enabled: false,
  start: vi.fn<() => Promise<boolean>>(),
  cancel: vi.fn(),
}
vi.mock('../hooks/useStreamingStt', () => ({
  get streamingSupported() { return stream.enabled },
  useStreamingStt: () => ({ recording: false, draining: false, start: stream.start, stop: vi.fn(), cancel: stream.cancel }),
}))

vi.mock('../api/client', () => ({ api: { sttTranscribe: vi.fn().mockResolvedValue({ text: '' }), sttPrewarm: vi.fn().mockResolvedValue({}) } }))

function makeStream() {
  const track = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic' }
  return { getAudioTracks: () => [track], getTracks: () => [track] }
}

class MockMediaRecorder {
  static isTypeSupported() { return true }
  state: 'inactive' | 'recording' = 'inactive'
  stream: unknown
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(s: unknown) { this.stream = s }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive'; this.onstop?.() }
}

class MockAudioContext {
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() { return { fftSize: 0, frequencyBinCount: 16, getByteTimeDomainData() {}, connect() {} } }
  close() { return Promise.resolve() }
}

let getUserMedia: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  stream.enabled = false
  stream.start.mockReset()
  stream.cancel.mockReset()
  getUserMedia = vi.fn()
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) },
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

async function loadHook() {
  return (await import('../hooks/useVoiceInput'))
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

describe('voice start deadline, batch capture', () => {
  it('gives up on a hung getUserMedia, says so, and lets the next press start', async () => {
    const { useVoiceInput, VOICE_START_DEADLINE_MS } = await loadHook()
    getUserMedia
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockImplementationOnce(() => Promise.resolve(makeStream()))
    const { result } = renderHook(() => useVoiceInput(vi.fn()))

    let settled = false
    act(() => { void result.current.start().then(() => { settled = true }) })
    await advance(VOICE_START_DEADLINE_MS - 1)
    expect(settled).toBe(false)
    expect(result.current.error).toBeNull()

    await advance(1)
    expect(settled).toBe(true)
    expect(result.current.error).toBe(i18nT(TIMED_OUT))
    expect(result.current.recording).toBe(false)

    await act(async () => { await result.current.start() })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(result.current.recording).toBe(true)
  })

  it('leaves a startup that reached capture alone', async () => {
    const { useVoiceInput, VOICE_START_DEADLINE_MS } = await loadHook()
    getUserMedia.mockImplementation(() => Promise.resolve(makeStream()))
    const { result } = renderHook(() => useVoiceInput(vi.fn()))

    await act(async () => { await result.current.start() })
    await advance(VOICE_START_DEADLINE_MS * 2)

    expect(result.current.recording).toBe(true)
    expect(result.current.error).toBeNull()
  })
})

describe('voice start deadline, streaming capture', () => {
  it('cancels a streaming startup that never reaches capture and says so', async () => {
    stream.enabled = true
    stream.start.mockImplementation(() => new Promise(() => {}))
    const { useVoiceInput, VOICE_START_DEADLINE_MS } = await loadHook()
    const { result } = renderHook(() => useVoiceInput(vi.fn(), { streaming: true }))

    let settled = false
    act(() => { void result.current.start().then(() => { settled = true }) })
    await advance(VOICE_START_DEADLINE_MS)

    expect(settled).toBe(true)
    expect(stream.cancel).toHaveBeenCalledTimes(1)
    expect(result.current.error).toBe(i18nT(TIMED_OUT))

    stream.start.mockImplementation(() => Promise.resolve(true))
    await act(async () => { await result.current.start() })
    expect(stream.start).toHaveBeenCalledTimes(2)
  })

  it('does not fire after a streaming startup that settled in time', async () => {
    stream.enabled = true
    stream.start.mockImplementation(() => Promise.resolve(true))
    const { useVoiceInput, VOICE_START_DEADLINE_MS } = await loadHook()
    const { result } = renderHook(() => useVoiceInput(vi.fn(), { streaming: true }))

    await act(async () => { await result.current.start() })
    // The deadline is cleared once the startup settles, not left to fire later.
    expect(vi.getTimerCount()).toBe(0)
    await advance(VOICE_START_DEADLINE_MS * 2)

    expect(stream.cancel).not.toHaveBeenCalled()
    expect(result.current.error).toBeNull()
  })

  it('does not fire for a startup the user already cancelled', async () => {
    stream.enabled = true
    stream.start.mockImplementation(() => new Promise(() => {}))
    const { useVoiceInput, VOICE_START_DEADLINE_MS } = await loadHook()
    const { result } = renderHook(() => useVoiceInput(vi.fn(), { streaming: true }))

    act(() => { void result.current.start() })
    act(() => { result.current.cancel() })
    await advance(VOICE_START_DEADLINE_MS)

    expect(stream.cancel).toHaveBeenCalledTimes(1)
    expect(result.current.error).toBeNull()
  })

  it('resolves the timeout message in the English catalog', () => {
    expect(i18nT(TIMED_OUT)).not.toBe(TIMED_OUT)
  })
})
