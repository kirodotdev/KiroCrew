import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

// Batch capture path only: stub the streaming hook so streamEnabled is false.
vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, start: vi.fn(), stop: vi.fn(), cancel: vi.fn() }),
}))

// Spyable transcription endpoint — the whole point of these tests is whether it
// is called (commit) or not (cancel/discard).
const sttTranscribe = vi.fn().mockResolvedValue({ text: 'hello world' })
vi.mock('../api/client', () => ({ api: { sttTranscribe: (...a: unknown[]) => sttTranscribe(...a) } }))

interface FakeTrack { stop: ReturnType<typeof vi.fn>; readyState: string; label: string }
function makeStream() {
  const track: FakeTrack = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic' }
  return { _track: track, getAudioTracks: () => [track], getTracks: () => [track] }
}

// Captures live instances so a test can feed audio + inspect state (push, not
// `= this`, to avoid the no-this-alias lint rule).
const recorders: MockMediaRecorder[] = []
const lastRecorderRef = () => recorders[recorders.length - 1] ?? null
class MockMediaRecorder {
  static isTypeSupported() { return true }
  state: 'inactive' | 'recording' = 'inactive'
  stream: unknown
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(stream: unknown) { this.stream = stream; recorders.push(this) }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive'; this.onstop?.() }
  /** Simulate a real recorder emitting a captured chunk big enough to transcribe. */
  feed(bytes = 200) { this.ondataavailable?.({ data: new Blob(['x'.repeat(bytes)]) }) }
}

class MockAudioContext {
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return { fftSize: 0, frequencyBinCount: 16, getByteTimeDomainData() {}, getByteFrequencyData() {}, connect() {} }
  }
  close() { return Promise.resolve() }
}

let getUserMedia: ReturnType<typeof vi.fn>

beforeEach(() => {
  recorders.length = 0
  sttTranscribe.mockClear()
  getUserMedia = vi.fn().mockResolvedValue(makeStream())
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

async function loadHook() {
  const mod = await import('../hooks/useVoiceInput')
  return mod.useVoiceInput
}

describe('useVoiceInput cancel releases the startup latch', () => {
  // The re-entrancy latch is held for the whole async startup. Cancelling while
  // `getUserMedia` is still awaiting the permission dialog used to leave it held
  // for as long as that dialog stayed open — nothing settles that await — so the
  // next press hit `if (startingRef.current) return` and recorded NOTHING. On a
  // push-to-talk binding that is an ordinary sequence: press, release during the
  // first-run permission prompt, press again.
  it('lets a replacement press start after a cancel mid-acquire', async () => {
    const useVoiceInput = await loadHook()
    let resolveFirst: (s: unknown) => void = () => {}
    getUserMedia
      .mockImplementationOnce(() => new Promise(r => { resolveFirst = r }))
      .mockImplementationOnce(() => Promise.resolve(makeStream()))
    const { result } = renderHook(() => useVoiceInput(vi.fn()))

    // Press: startup begins and parks on the permission dialog.
    act(() => { void result.current.start() })
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(result.current.recording).toBe(false)

    // Release while the dialog is still open.
    act(() => { result.current.cancel() })

    // The replacement press must actually acquire and record.
    await act(async () => { await result.current.start() })
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(result.current.recording).toBe(true))

    // The abandoned acquire now resolves. It must not disturb the live session:
    // its own teardown would otherwise null the warm promise, drop ownership and
    // surface a mic error for a recording that is running fine.
    await act(async () => { resolveFirst(makeStream()); await Promise.resolve() })
    expect(result.current.recording).toBe(true)
    expect(result.current.error).toBeNull()
  })
})

describe('useVoiceInput cancel (Esc discard)', () => {
  it('DROPS the audio without transcribing', async () => {
    const useVoiceInput = await loadHook()
    const onText = vi.fn()
    const { result } = renderHook(() => useVoiceInput(onText))

    await act(async () => { await result.current.toggle() }) // start
    await waitFor(() => expect(result.current.recording).toBe(true))
    act(() => { lastRecorderRef()?.feed() }) // captured a real chunk

    act(() => { result.current.cancel() })

    await waitFor(() => expect(result.current.recording).toBe(false))
    expect(sttTranscribe).not.toHaveBeenCalled() // discarded, never sent
    expect(onText).not.toHaveBeenCalled()
    expect(result.current.transcribing).toBe(false)
  })

  it('by contrast, stopping via toggle COMMITS (transcribes) the same audio', async () => {
    const useVoiceInput = await loadHook()
    const onText = vi.fn()
    const { result } = renderHook(() => useVoiceInput(onText))

    await act(async () => { await result.current.toggle() }) // start
    await waitFor(() => expect(result.current.recording).toBe(true))
    act(() => { lastRecorderRef()?.feed() })

    await act(async () => { result.current.toggle() }) // stop -> transcribe

    // The discriminator vs. the discard test above: committing sends the audio.
    await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(1))
  })
})
