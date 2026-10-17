/**
 * A batch transcription that fails keeps its recording in the tab, so the user
 * can retry it, save it, or discard it instead of losing what they said. The
 * gateway deletes its copy of the upload on every exit, which leaves the
 * browser's blob as the only one.
 *
 * Drives the real hook through record -> stop -> a failed `/api/stt` answer and
 * asserts what the error notice can do next: Retry on a failure that can clear
 * by itself (5xx, no answer at all), only Save and Discard when the gateway
 * refused the audio (413, 422), and the release points the audio has.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'

// Batch capture path only: stub the streaming hook so streamEnabled is false.
vi.mock('../hooks/useStreamingStt', () => ({
  streamingSupported: false,
  useStreamingStt: () => ({ recording: false, start: vi.fn(), stop: vi.fn(), cancel: vi.fn() }),
}))

// Each call takes the next scripted answer: a value resolves, an Error rejects.
const answers: unknown[] = []
const sttTranscribe = vi.fn((..._args: unknown[]) => {
  const next = answers.shift()
  return next instanceof Error ? Promise.reject(next) : Promise.resolve(next)
})
vi.mock('../api/client', () => ({ api: { sttTranscribe: (...a: unknown[]) => sttTranscribe(...a) } }))

const downloadBlob = vi.fn()
vi.mock('../utils/download', () => ({ downloadBlob: (...a: unknown[]) => downloadBlob(...a) }))

function makeStream() {
  const track = { stop: vi.fn(), readyState: 'live', label: 'Mock Mic' }
  return { getAudioTracks: () => [track], getTracks: () => [track] }
}

const recorders: MockMediaRecorder[] = []
const lastRecorder = () => recorders[recorders.length - 1] ?? null
class MockMediaRecorder {
  static isTypeSupported() { return true }
  state: 'inactive' | 'recording' = 'inactive'
  stream: unknown
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(stream: unknown) { this.stream = stream; recorders.push(this) }
  start() { this.state = 'recording' }
  stop() { this.state = 'inactive'; this.onstop?.() }
  feed(bytes = 200) { this.ondataavailable?.({ data: new Blob(['x'.repeat(bytes)]) }) }
}

class MockAudioContext {
  createMediaStreamSource() { return { connect() {} } }
  createAnalyser() {
    return { fftSize: 0, frequencyBinCount: 16, getByteTimeDomainData() {}, getByteFrequencyData() {}, connect() {} }
  }
  close() { return Promise.resolve() }
}

beforeEach(() => {
  recorders.length = 0
  answers.length = 0
  sttTranscribe.mockClear()
  downloadBlob.mockClear()
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockImplementation(() => Promise.resolve(makeStream())), enumerateDevices: vi.fn().mockResolvedValue([]) },
    configurable: true,
    writable: true,
  })
  vi.stubGlobal('MediaRecorder', MockMediaRecorder as unknown as typeof MediaRecorder)
  vi.stubGlobal('AudioContext', MockAudioContext as unknown as typeof AudioContext)
  // The inbox holds module state; a fresh registry per test keeps one test's
  // kept recording out of the next.
  vi.resetModules()
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Built after `vi.resetModules()`, so it is the class the freshly imported hook checks against. */
async function apiError(status: number, message: string) {
  const { ApiError } = await import('../api/apiError')
  return new ApiError(status, message)
}

type Hook = ReturnType<typeof import('../hooks/useVoiceInput')['useVoiceInput']>

async function mount(onText = vi.fn()) {
  const { useVoiceInput } = await import('../hooks/useVoiceInput')
  const view = renderHook(() => useVoiceInput(onText, { sessionId: 'chat-a', ownsSession: id => id === 'chat-a', acceptsUnowned: false }))
  return { ...view, onText }
}

/** Record an utterance, stop, and let the scripted `/api/stt` answer settle. */
async function dictate(result: { current: Hook }, answer: unknown) {
  answers.push(answer)
  const calls = sttTranscribe.mock.calls.length
  await act(async () => { await result.current.start() })
  await waitFor(() => expect(result.current.recording).toBe(true))
  act(() => { lastRecorder()?.feed() })
  await act(async () => { result.current.stop() })
  await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(calls + 1))
  await waitFor(() => expect(result.current.transcribing).toBe(false))
}

describe('useVoiceInput — a failed transcription keeps the recording', () => {
  it.each([
    ['a 500', 500],
    ['a 503', 503],
    ['a network failure', 0],
  ])('offers Retry, Save and Discard after %s', async (_label, status) => {
    const { result } = await mount()
    await dictate(result, status ? await apiError(status, 'transcription failed') : new TypeError('Failed to fetch'))
    expect(result.current.error).toBeTruthy()
    expect(result.current.keptRecording).not.toBeNull()
    expect(result.current.keptRecording?.retryable).toBe(true)
  })

  it.each([
    ['a 413', 413],
    ['a 422', 422],
  ])('offers only Save and Discard after %s', async (_label, status) => {
    const { result } = await mount()
    await dictate(result, await apiError(status, 'audio refused'))
    expect(result.current.keptRecording?.retryable).toBe(false)
    // Retry is not offered, and pressing it anyway posts nothing.
    act(() => { result.current.keptRecording?.retry() })
    expect(sttTranscribe).toHaveBeenCalledTimes(1)
    expect(result.current.keptRecording).not.toBeNull()
  })

  it('a successful retry re-posts the same audio and lands in the owning composer', async () => {
    const { result, onText } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const [blob, ext] = sttTranscribe.mock.calls[0]

    answers.push({ text: 'hello world' })
    await act(async () => { result.current.keptRecording?.retry() })
    await waitFor(() => expect(onText).toHaveBeenCalledWith('hello world', 'chat-a', 'batch'))
    expect(sttTranscribe).toHaveBeenCalledTimes(2)
    expect(sttTranscribe.mock.calls[1][0]).toBe(blob)
    expect(sttTranscribe.mock.calls[1][1]).toBe(ext)
    expect(result.current.error).toBeNull()
    expect(result.current.keptRecording).toBeNull()
  })

  it('a retry that fails again keeps the same audio for another try', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const [blob] = sttTranscribe.mock.calls[0]

    answers.push(await apiError(503, 'retry the upload'))
    await act(async () => { result.current.keptRecording?.retry() })
    await waitFor(() => expect(result.current.keptRecording).not.toBeNull())
    expect(result.current.error).toBeTruthy()

    answers.push({ text: 'third time' })
    await act(async () => { result.current.keptRecording?.retry() })
    await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(3))
    expect(sttTranscribe.mock.calls[2][0]).toBe(blob)
  })

  it('Save downloads the recording with its own extension and keeps it', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const [blob] = sttTranscribe.mock.calls[0]

    act(() => { result.current.keptRecording?.save() })
    expect(downloadBlob).toHaveBeenCalledTimes(1)
    expect(downloadBlob.mock.calls[0][0]).toBe(blob)
    expect(downloadBlob.mock.calls[0][1]).toMatch(/^recording-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.webm$/)
    // Saving does not end the offer: a retry is still worth having.
    expect(result.current.keptRecording).not.toBeNull()
  })

  it('Discard lets the recording go and closes the notice', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const retry = result.current.keptRecording!.retry

    act(() => { result.current.keptRecording?.discard() })
    expect(result.current.keptRecording).toBeNull()
    expect(result.current.error).toBeNull()
    // A stale handle to the old offer has nothing left to post.
    act(() => { retry() })
    expect(sttTranscribe).toHaveBeenCalledTimes(1)
  })

  it('dismissing the error lets the recording go too', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const retry = result.current.keptRecording!.retry
    act(() => { result.current.clearError() })
    expect(result.current.keptRecording).toBeNull()
    act(() => { retry() })
    expect(sttTranscribe).toHaveBeenCalledTimes(1)
  })

  it('a new recording lets the kept one go once it is capturing', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const retry = result.current.keptRecording!.retry

    await act(async () => { await result.current.start() })
    await waitFor(() => expect(result.current.recording).toBe(true))
    expect(result.current.keptRecording).toBeNull()
    expect(result.current.error).toBeNull()
    act(() => { retry() })
    expect(sttTranscribe).toHaveBeenCalledTimes(1)
  })

  it('a microphone that fails to start leaves the kept recording reachable', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))
    const kept = result.current.error

    // The only microphone is gone: the next start fails before capturing.
    ;(navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mockImplementation(
      () => Promise.reject(new DOMException('Requested device not found', 'NotFoundError')),
    )
    await act(async () => { await result.current.start() })
    expect(result.current.recording).toBe(false)
    // The microphone failure is shown first, and the recording is not lost.
    expect(result.current.error).toBeTruthy()
    expect(result.current.error).not.toBe(kept)
    expect(result.current.keptRecording).toBeNull()

    act(() => { result.current.clearError() })
    expect(result.current.error).toBe(kept)
    expect(result.current.keptRecording?.retryable).toBe(true)
    answers.push({ text: 'still here' })
    await act(async () => { result.current.keptRecording?.retry() })
    await waitFor(() => expect(sttTranscribe).toHaveBeenCalledTimes(2))
    expect(sttTranscribe.mock.calls[1][0]).toBe(sttTranscribe.mock.calls[0][0])
  })

  it('refuses a retry while the next recording is still starting', async () => {
    const { result } = await mount()
    await dictate(result, await apiError(500, 'transcription failed'))

    // The microphone grant is slow: startup is pending, nothing captures yet.
    let grant: (s: unknown) => void = () => {}
    ;(navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(resolve => { grant = resolve }),
    )
    let starting: Promise<void> = Promise.resolve()
    act(() => { starting = result.current.start() })
    expect(result.current.keptRecording).not.toBeNull()
    act(() => { result.current.keptRecording?.retry() })
    expect(sttTranscribe).toHaveBeenCalledTimes(1)

    // The grant arrives: capture starts and ends the offer for the old audio.
    await act(async () => { grant(makeStream()); await starting })
    await waitFor(() => expect(result.current.recording).toBe(true))
    expect(result.current.keptRecording).toBeNull()
    expect(sttTranscribe).toHaveBeenCalledTimes(1)
  })

  it('offers the kept recording again after the page unmounts and comes back', async () => {
    const first = await mount()
    await dictate(first.result, await apiError(500, 'transcription failed'))
    const [blob] = sttTranscribe.mock.calls[0]
    // A trip to Settings and back.
    first.unmount()
    const { useVoiceInput } = await import('../hooks/useVoiceInput')
    const onText = vi.fn()
    const second = renderHook(() => useVoiceInput(onText, { sessionId: 'chat-a', ownsSession: id => id === 'chat-a', acceptsUnowned: false }))
    await waitFor(() => expect(second.result.current.keptRecording).not.toBeNull())
    expect(second.result.current.error).toBeTruthy()

    answers.push({ text: 'after the trip' })
    await act(async () => { second.result.current.keptRecording?.retry() })
    await waitFor(() => expect(onText).toHaveBeenCalledWith('after the trip', 'chat-a', 'batch'))
    expect(sttTranscribe.mock.calls[1][0]).toBe(blob)
  })

  it('clears the notice on every composer showing it when one of them retries', async () => {
    const a = await mount()
    const { useVoiceInput } = await import('../hooks/useVoiceInput')
    const b = renderHook(() => useVoiceInput(vi.fn(), { sessionId: 'chat-b', ownsSession: id => id === 'chat-b', acceptsUnowned: false }))
    await dictate(a.result, await apiError(500, 'transcription failed'))
    await waitFor(() => expect(b.result.current.keptRecording).not.toBeNull())

    answers.push({ text: 'from b' })
    await act(async () => { b.result.current.keptRecording?.retry() })
    // The transcript goes to the session that spoke, not the composer pressed.
    await waitFor(() => expect(a.onText).toHaveBeenCalledWith('from b', 'chat-a', 'batch'))
    expect(a.result.current.keptRecording).toBeNull()
    expect(b.result.current.keptRecording).toBeNull()
  })
})
