import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

// Isolate the batch capture path: stub the streaming hook so streamEnabled is
// always false and no WebSocket/Transcribe session is involved.
vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, start: vi.fn(), stop: vi.fn() }),
}))

interface FakeTrack { stop: ReturnType<typeof vi.fn>; readyState: string; label: string }
function makeStream() {
  const track: FakeTrack = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic' }
  return { _track: track, getAudioTracks: () => [track], getTracks: () => [track] }
}

class MockMediaRecorder {
  static isTypeSupported() { return true }
  state: 'inactive' | 'recording' = 'inactive'
  stream: unknown
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(stream: unknown) { this.stream = stream }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive'; this.onstop?.() }
}

class MockAudioContext {
  createMediaStreamSource() { return { connect() {} } }
  // Both read methods are stubbed: the level meter reads the time domain for
  // RMS and the frequency domain for the shader's spectral centroid, and a real
  // AnalyserNode always exposes both.
  createAnalyser() {
    return {
      fftSize: 0,
      frequencyBinCount: 16,
      getByteTimeDomainData() {},
      getByteFrequencyData() {},
      connect() {},
    }
  }
  close() { return Promise.resolve() }
}

let getUserMedia: ReturnType<typeof vi.fn>
let currentStream: ReturnType<typeof makeStream>

beforeEach(() => {
  currentStream = makeStream()
  getUserMedia = vi.fn().mockResolvedValue(currentStream)
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
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// Imported after globals are stubbed so the module-load `voiceInputSupported`
// const (which probes MediaRecorder + getUserMedia) evaluates to true.
async function loadHook() {
  const mod = await import('../hooks/useVoiceInput')
  return mod.useVoiceInput
}

/** Set the saved mic preference through the same module the hook reads. */
async function setPreferredMicId(id: string) {
  const mic = await import('../hooks/mic')
  mic.setPreferredMicId(id)
}

describe('useVoiceInput mic pre-warming', () => {
  it('prewarm() acquires the mic stream', async () => {
    const useVoiceInput = await loadHook()
    const { result } = renderHook(() => useVoiceInput(() => {}))
    await act(async () => { result.current.prewarm() })
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))
  })

  it('start() after prewarm reuses the warmed stream (single getUserMedia)', async () => {
    const useVoiceInput = await loadHook()
    const { result } = renderHook(() => useVoiceInput(() => {}))
    await act(async () => { result.current.prewarm() })
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))
    await act(async () => { result.current.toggle() })
    await waitFor(() => expect(result.current.recording).toBe(true))
    // The pre-warmed stream is reused — no second acquisition.
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('start() without prewarm acquires the mic and begins recording', async () => {
    const useVoiceInput = await loadHook()
    const { result } = renderHook(() => useVoiceInput(() => {}))
    await act(async () => { result.current.toggle() })
    await waitFor(() => expect(result.current.recording).toBe(true))
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('surfaces a humanized error when the mic is denied', async () => {
    const useVoiceInput = await loadHook()
    getUserMedia.mockRejectedValueOnce(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    const { result } = renderHook(() => useVoiceInput(() => {}))
    await act(async () => { result.current.toggle() })
    await waitFor(() => expect(result.current.error).toMatch(/permission denied/i))
    expect(result.current.recording).toBe(false)
  })

  it('releases a pre-warmed stream if recording never starts (idle timeout)', async () => {
    const useVoiceInput = await loadHook()
    const { result } = renderHook(() => useVoiceInput(() => {}))
    vi.useFakeTimers()
    try {
      act(() => { result.current.prewarm() })
      // Flush the getUserMedia microtask so warmRef + level meter are attached.
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      expect(getUserMedia).toHaveBeenCalledTimes(1)
      act(() => { vi.advanceTimersByTime(15000) })
      expect(currentStream._track.stop).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops a mic stream that resolves after the hook unmounts (no leak)', async () => {
    const useVoiceInput = await loadHook()
    let resolveStream: (s: unknown) => void = () => {}
    const pending = new Promise<unknown>(res => { resolveStream = res })
    getUserMedia.mockReturnValueOnce(pending)
    const late = makeStream()
    const { result, unmount } = renderHook(() => useVoiceInput(() => {}))
    act(() => { result.current.prewarm() })   // getUserMedia now in-flight
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    unmount()                                  // stopStream() nulls warmPromiseRef
    // getUserMedia resolves only after unmount -> must be torn down, not leaked.
    await act(async () => { resolveStream(late); await Promise.resolve(); await Promise.resolve() })
    expect(late._track.stop).toHaveBeenCalled()
  })

  it('reuses a pre-warmed stream that fell back to another device', async () => {
    // The chosen mic was busy, so `ideal` silently gave us the default. The track is
    // then permanently not-the-preference — a track-based staleness test would
    // discard a working stream and re-acquire on EVERY press, getting the same
    // fallback back each time. Re-acquiring cannot free a busy device.
    const useVoiceInput = await loadHook()
    await setPreferredMicId('airpods')
    const fellBack = makeStream()
    fellBack._track.getSettings = () => ({ deviceId: 'builtin' })
    getUserMedia.mockResolvedValueOnce(fellBack)
    const { result } = renderHook(() => useVoiceInput(() => {}))
    await act(async () => { await result.current.prewarm() })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    await act(async () => { await result.current.prewarm() })
    expect(getUserMedia).toHaveBeenCalledTimes(1) // reused, not re-acquired
  })

  it('drops a pre-warmed stream when the preference changed since acquisition', async () => {
    // The other side of the same test: a preference change (e.g. from Settings,
    // which does not touch the warm refs) must still invalidate.
    const useVoiceInput = await loadHook()
    await setPreferredMicId('builtin')
    const first = makeStream()
    getUserMedia.mockResolvedValueOnce(first)
    const { result } = renderHook(() => useVoiceInput(() => {}))
    await act(async () => { await result.current.prewarm() })
    await setPreferredMicId('airpods')
    const second = makeStream()
    getUserMedia.mockResolvedValueOnce(second)
    await act(async () => { await result.current.prewarm() })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(first._track.stop).toHaveBeenCalled()
  })

  it('cancels an in-flight pre-warm when the device is switched mid-acquisition', async () => {
    // acquireWarm returns warmPromiseRef.current BEFORE any staleness check, so a
    // getUserMedia still resolving from the OLD device would be handed straight to
    // the next recording — the defect class this picker exists to remove, in a
    // ~50-200ms window. switchDevice must null the promise ref, not just the
    // settled stream.
    const useVoiceInput = await loadHook()
    let resolveOld: (s: unknown) => void = () => {}
    const pending = new Promise<unknown>(res => { resolveOld = res })
    getUserMedia.mockReturnValueOnce(pending)
    const oldStream = makeStream()
    const { result } = renderHook(() => useVoiceInput(() => {}))

    act(() => { result.current.prewarm() })   // old device's getUserMedia in-flight
    expect(getUserMedia).toHaveBeenCalledTimes(1)

    await act(async () => { await result.current.switchDevice('new-device-id') })
    // The old acquisition lands only now: it must be torn down, never reused.
    await act(async () => { resolveOld(oldStream); await Promise.resolve(); await Promise.resolve() })
    expect(oldStream._track.stop).toHaveBeenCalled()

    // The next press must acquire afresh rather than reuse the cancelled promise.
    const fresh = makeStream()
    getUserMedia.mockResolvedValueOnce(fresh)
    await act(async () => { await result.current.prewarm() })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
  })
})
