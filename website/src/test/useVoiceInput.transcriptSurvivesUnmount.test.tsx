/**
 * Regression tests for issue #1882: navigating away from Chat cancelled an
 * active voice transcription.
 *
 * The `/api/stt` request survives the unmount on its own — it is a plain fetch.
 * What was lost is its DELIVERY: the callback it resolved into belonged to the
 * unmounted page, so the transcript went nowhere. These tests drive the real
 * hook through unmount → remount and assert the transcript (and the busy
 * indicator, and a failure) reach the instance that is mounted when the request
 * settles.
 *
 * They also pin the SCOPE of the fix: leaving Chat must still stop the
 * microphone. Keeping the mic alive across navigation would leave a recording
 * running with no indicator and no stop control, since both live in Chat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

// Batch capture path only: stub the streaming hook so streamEnabled is false.
vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, start: vi.fn(), stop: vi.fn(), cancel: vi.fn() }),
}))

// Transcription is resolved by hand so a test can hold the request open across
// the unmount — the exact window the bug lived in.
let settleStt: (res: { text?: string; error?: string }) => void = () => {}
const sttTranscribe = vi.fn(() => new Promise(resolve => { settleStt = resolve as typeof settleStt }))
vi.mock('../api/client', () => ({ api: { sttTranscribe: (...a: unknown[]) => sttTranscribe(...a) } }))

interface FakeTrack { stop: ReturnType<typeof vi.fn>; readyState: string; label: string }
let lastTrack: FakeTrack | null = null
function makeStream() {
  const track: FakeTrack = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic' }
  lastTrack = track
  return { _track: track, getAudioTracks: () => [track], getTracks: () => [track] }
}

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
  lastTrack = null
  sttTranscribe.mockClear()
  getUserMedia = vi.fn().mockImplementation(() => Promise.resolve(makeStream()))
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
  vi.stubGlobal('MediaRecorder', MockMediaRecorder as unknown as typeof MediaRecorder)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  // The inbox holds module state; a fresh registry per test keeps one test's
  // pending transcript out of the next.
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

/** Record a real utterance and press stop, leaving the transcription in flight. */
async function dictateAndStop(hook: { current: { start: () => Promise<void>; stop: () => void; recording: boolean } }) {
  await act(async () => { await hook.current.start() })
  await waitFor(() => expect(hook.current.recording).toBe(true))
  act(() => { lastRecorderRef()?.feed() })
  act(() => { hook.current.stop() })
  await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(1))
}

describe('useVoiceInput — a transcription survives the page unmounting', () => {
  it('releases the busy state on the recording instance when it settles', async () => {
    const useVoiceInput = await loadHook()
    const onText = vi.fn()
    const { result } = renderHook(() => useVoiceInput(onText, { sessionId: 'chat-a' }))
    await dictateAndStop(result as never)
    expect(result.current.transcribing).toBe(true)
    expect(result.current.sessionOwner).toBe('chat-a')

    await act(async () => { settleStt({ text: 'hello world' }) })

    // The subscriber is the ONLY release path, so the ordinary
    // record-stop-transcribe round trip has to clear through it.
    await waitFor(() => expect(result.current.transcribing).toBe(false))
    expect(result.current.sessionOwner).toBeNull()
    expect(onText).toHaveBeenCalledWith('hello world', 'chat-a', 'batch')
  })

  it('delivers a transcript that settles after unmount to the next instance', async () => {
    const useVoiceInput = await loadHook()
    const onTextA = vi.fn()
    const first = renderHook(() => useVoiceInput(onTextA, { sessionId: 'chat-a' }))
    await dictateAndStop(first.result as never)

    // Navigate away from Chat while the request is still open.
    first.unmount()

    const onTextB = vi.fn()
    renderHook(() => useVoiceInput(onTextB, { sessionId: 'chat-a' }))

    await act(async () => { settleStt({ text: 'hello world' }) })

    // The mounted instance receives it, still attributed to the slot that spoke,
    // and tagged as batch so the caller's streaming-only disarm flags don't
    // suppress it if streaming was switched on while Chat was away.
    await waitFor(() => expect(onTextB).toHaveBeenCalledWith('hello world', 'chat-a', 'batch'))
    expect(onTextA).not.toHaveBeenCalled()
  })

  it('restores the transcribing indicator on the instance that returns', async () => {
    const useVoiceInput = await loadHook()
    const first = renderHook(() => useVoiceInput(vi.fn(), { sessionId: 'chat-a' }))
    await dictateAndStop(first.result as never)
    expect(first.result.current.transcribing).toBe(true)
    first.unmount()

    // Coming back mid-request must show the session as still busy, not idle.
    const second = renderHook(() => useVoiceInput(vi.fn(), { sessionId: 'chat-a' }))
    expect(second.result.current.transcribing).toBe(true)
    expect(second.result.current.sessionOwner).toBe('chat-a')

    await act(async () => { settleStt({ text: 'hello world' }) })
    await waitFor(() => expect(second.result.current.transcribing).toBe(false))
    expect(second.result.current.sessionOwner).toBeNull()
  })

  it('surfaces a failure that settles after unmount instead of hanging busy', async () => {
    const useVoiceInput = await loadHook()
    const onTextA = vi.fn()
    const first = renderHook(() => useVoiceInput(onTextA, { sessionId: 'chat-a' }))
    await dictateAndStop(first.result as never)
    first.unmount()

    const onTextB = vi.fn()
    const second = renderHook(() => useVoiceInput(onTextB, { sessionId: 'chat-a' }))
    await act(async () => { settleStt({ error: 'model unavailable' }) })

    await waitFor(() => expect(second.result.current.error).toBeTruthy())
    expect(second.result.current.transcribing).toBe(false)
    expect(onTextB).not.toHaveBeenCalled()
  })
  it('does not blank a new recording when an older transcription settles', async () => {
    const useVoiceInput = await loadHook()
    const first = renderHook(() => useVoiceInput(vi.fn(), { sessionId: 'chat-a' }))
    await dictateAndStop(first.result as never)
    first.unmount()

    // Back in Chat, in a different session, the user starts dictating again while
    // the earlier request is still open. Its settlement belongs to a session that
    // is over, so it must not clear the live recording's owner — that owner is what
    // gates the mic indicator and the stop control for a microphone that is
    // actually running.
    const onTextB = vi.fn()
    const second = renderHook(() => useVoiceInput(onTextB, { sessionId: 'chat-b' }))
    await act(async () => { await second.result.current.start() })
    await waitFor(() => expect(second.result.current.recording).toBe(true))

    await act(async () => { settleStt({ text: 'hello world' }) })

    expect(second.result.current.recording).toBe(true)
    expect(second.result.current.sessionOwner).toBe('chat-b')
    // The older transcript is still handed over, attributed to its own slot.
    await waitFor(() => expect(onTextB).toHaveBeenCalledWith('hello world', 'chat-a', 'batch'))
  })


  it('drains a transcript that settled while no instance was mounted', async () => {
    const useVoiceInput = await loadHook()
    const onTextA = vi.fn()
    const first = renderHook(() => useVoiceInput(onTextA, { sessionId: 'chat-a' }))
    await dictateAndStop(first.result as never)
    first.unmount()

    // Settles with Chat closed: nothing can take it, so it waits.
    await act(async () => { settleStt({ text: 'hello world' }) })
    expect(onTextA).not.toHaveBeenCalled()

    // Mounting hands it over — but only after the mount pass has committed, so
    // the page's own prefill/draft effects are not raced.
    const onTextB = vi.fn()
    renderHook(() => useVoiceInput(onTextB, { sessionId: 'chat-a' }))
    expect(onTextB).not.toHaveBeenCalled()
    await act(async () => { await Promise.resolve() })
    expect(onTextB).toHaveBeenCalledWith('hello world', 'chat-a', 'batch')
  })

  it('hands a pending drain to the instance that is current when it runs', async () => {
    const useVoiceInput = await loadHook()
    const first = renderHook(() => useVoiceInput(vi.fn(), { sessionId: 'chat-a' }))
    await dictateAndStop(first.result as never)
    first.unmount()
    await act(async () => { settleStt({ text: 'hello world' }) })

    // Subscribe, tear down and resubscribe before the deferred drain runs — the
    // shape StrictMode produces on every mount in development. The text must
    // follow the live instance instead of being stranded by the subscription
    // that happened to schedule the drain.
    const onTextB = vi.fn()
    const second = renderHook(() => useVoiceInput(onTextB, { sessionId: 'chat-a' }))
    second.unmount()
    const onTextC = vi.fn()
    renderHook(() => useVoiceInput(onTextC, { sessionId: 'chat-a' }))

    await act(async () => { await Promise.resolve() })
    expect(onTextC).toHaveBeenCalledWith('hello world', 'chat-a', 'batch')
    expect(onTextB).not.toHaveBeenCalled()
  })

  it('still stops the microphone when the page unmounts mid-recording', async () => {
    const useVoiceInput = await loadHook()
    const { result, unmount } = renderHook(() => useVoiceInput(vi.fn(), { sessionId: 'chat-a' }))
    await act(async () => { await result.current.start() })
    await waitFor(() => expect(result.current.recording).toBe(true))
    const track = lastTrack
    act(() => { lastRecorderRef()?.feed() })

    unmount()

    // The mic is released and the recorder is closed — leaving Chat never leaves
    // capture running behind a UI that no longer exists. The utterance already
    // captured is committed to transcription rather than discarded.
    expect(track?.stop).toHaveBeenCalled()
    expect(lastRecorderRef()?.state).toBe('inactive')
    await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(1))
  })
})
