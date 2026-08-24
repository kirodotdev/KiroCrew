/**
 * The post-meeting batch RECORDING hook, driven over a mocked MediaRecorder and
 * getUserMedia.
 *
 * Approach A: the hook records the mic while the meeting is active and uploads
 * the ONE blob when the meeting stops — no live transcript. These tests assert
 * the load-bearing order (record → stop → upload COMPLETES → caller may then
 * call the stop endpoint), the typed-only path (no recorder → no upload), and
 * the mic-denied path (reported, meeting still stoppable).
 *
 * The MediaRecorder / getUserMedia doubles follow the harness style in
 * `UseMeetingTranscriptionCoverage.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

type HookModule = typeof import('../apps/meetings/hooks/useMeetingRecording')
type ApiModule = typeof import('../apps/meetings/api')

const recorders: MockRecorder[] = []
const lastRecorder = () => recorders[recorders.length - 1]

class MockRecorder {
  static supported = new Set<string>([
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ])
  static isTypeSupported(t: string) { return MockRecorder.supported.has(t) }

  state: 'inactive' | 'recording' = 'inactive'
  readonly mimeType: string
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  startCalls: number[] = []

  constructor(_stream: MediaStream, opts?: { mimeType?: string }) {
    this.mimeType = opts?.mimeType ?? ''
    recorders.push(this)
  }

  start(timeslice?: number) {
    this.state = 'recording'
    this.startCalls.push(timeslice ?? 0)
  }

  /** Push one buffered chunk, as the real recorder does on its timeslice. */
  emit(bytes = 1024) {
    this.ondataavailable?.({ data: new Blob([new Uint8Array(bytes)], { type: this.mimeType }) })
  }

  stop() {
    this.state = 'inactive'
    // Real MediaRecorder fires a trailing dataavailable then onstop, async.
    setTimeout(() => this.onstop?.(), 0)
  }
}

const stoppedTracks: string[] = []

function makeStream(label = 'mic') {
  const track = { stop: () => { stoppedTracks.push(label) }, readyState: 'live' }
  return { getAudioTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream
}

let getUserMedia: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetModules()
  recorders.length = 0
  stoppedTracks.length = 0
  MockRecorder.supported = new Set([
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ])
  getUserMedia = vi.fn().mockResolvedValue(makeStream())
  vi.stubGlobal('MediaRecorder', MockRecorder as unknown as typeof MediaRecorder)
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

interface Harness {
  mod: HookModule
  api: ApiModule
  upload: ReturnType<typeof vi.fn>
  onError: ReturnType<typeof vi.fn>
  hook: ReturnType<typeof renderHook<ReturnType<HookModule['useMeetingRecording']>, unknown>>
  errors: () => string[]
}

async function mount(): Promise<Harness> {
  const api = await import('../apps/meetings/api')
  const mod = await import('../apps/meetings/hooks/useMeetingRecording')
  const upload = vi.fn().mockResolvedValue({ ok: true })
  vi.spyOn(api.meetingsApi, 'uploadAudio').mockImplementation(
    (id: string, blob: Blob) => upload(id, blob) as Promise<{ ok: boolean }>,
  )
  const onError = vi.fn()
  const hook = renderHook(() => mod.useMeetingRecording({ meetingId: 'meet-1', onError }))
  return {
    mod,
    api,
    upload,
    onError,
    hook,
    errors: () => onError.mock.calls.map(c => String(c[0])),
  }
}

async function startRecording(h: Harness) {
  await act(async () => { void h.hook.result.current.start() })
  await flush()
  return lastRecorder()
}

describe('useMeetingRecording — capture', () => {
  it('acquires the mic and starts a recorder with a supported opus mime type', async () => {
    const h = await mount()
    const rec = await startRecording(h)

    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
    expect(rec.mimeType).toBe('audio/webm;codecs=opus')
    expect(rec.startCalls).toHaveLength(1)
    expect(rec.startCalls[0]).toBeGreaterThan(0) // a timeslice was passed
    expect(h.hook.result.current.recording).toBe(true)
    expect(h.errors()).toEqual([])
  })

  it('falls back to a supported type when opus/webm is unavailable', async () => {
    const h = await mount()
    MockRecorder.supported = new Set(['audio/mp4'])
    const rec = await startRecording(h)
    expect(rec.mimeType).toBe('audio/mp4')
  })

  it('lets the browser choose when no candidate type is supported', async () => {
    const h = await mount()
    MockRecorder.supported = new Set()
    const rec = await startRecording(h)
    // Empty string => constructed without an explicit mimeType.
    expect(rec.mimeType).toBe('')
  })

  it('does not open a second recorder while one is already live', async () => {
    const h = await mount()
    await startRecording(h)
    await startRecording(h)
    expect(recorders).toHaveLength(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('collapses two starts that race inside the getUserMedia await', async () => {
    const h = await mount()
    await act(async () => {
      void h.hook.result.current.start()
      void h.hook.result.current.start()
    })
    await flush()
    expect(recorders).toHaveLength(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('reports a denied microphone and stays inactive', async () => {
    const h = await mount()
    getUserMedia.mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    await startRecording(h)

    expect(h.errors()).toEqual(['microphone'])
    expect(recorders).toHaveLength(0)
    expect(h.hook.result.current.recording).toBe(false)
  })

  it('reports when MediaRecorder is entirely unsupported', async () => {
    vi.stubGlobal('MediaRecorder', undefined)
    vi.resetModules()
    const h = await mount()
    expect(h.hook.result.current.supported).toBe(false)

    await act(async () => { await h.hook.result.current.start() })
    expect(h.errors()).toEqual(['unsupported'])
    expect(getUserMedia).not.toHaveBeenCalled()
  })
})

describe('useMeetingRecording — stop and upload', () => {
  it('assembles the recorded chunks and uploads once, resolving after upload', async () => {
    const h = await mount()
    const rec = await startRecording(h)

    await act(async () => { rec.emit(1000); rec.emit(500) })

    let uploaded: boolean | undefined
    await act(async () => {
      const p = h.hook.result.current.stopAndUpload().then(v => { uploaded = v })
      await vi.advanceTimersByTimeAsync(1)
      await p
    })

    expect(uploaded).toBe(true)
    expect(h.upload).toHaveBeenCalledTimes(1)
    const [id, blob] = h.upload.mock.calls[0]
    expect(id).toBe('meet-1')
    expect(blob).toBeInstanceOf(Blob)
    expect((blob as Blob).size).toBe(1500)
    expect(h.hook.result.current.recording).toBe(false)
    expect(stoppedTracks).toEqual(['mic'])
  })

  it('waits for the recorder to flush its final chunk before assembling', async () => {
    const h = await mount()
    const rec = await startRecording(h)
    await act(async () => { rec.emit(2048) })

    // stop() flips state and schedules onstop asynchronously; the promise must
    // not resolve until that fires.
    let settled = false
    await act(async () => {
      void h.hook.result.current.stopAndUpload().then(() => { settled = true })
    })
    expect(settled).toBe(false)

    await flush()
    expect(settled).toBe(true)
    expect(h.upload).toHaveBeenCalledTimes(1)
  })

  it('reports an upload failure but still resolves so the meeting can stop', async () => {
    const h = await mount()
    const rec = await startRecording(h)
    await act(async () => { rec.emit(1024) })
    h.upload.mockRejectedValue(new h.api.MeetingsApiError('boom', 500))

    let uploaded: boolean | undefined
    await act(async () => {
      const p = h.hook.result.current.stopAndUpload().then(v => { uploaded = v })
      await vi.advanceTimersByTimeAsync(1)
      await p
    })

    expect(uploaded).toBe(false)
    expect(h.errors()).toEqual(['upload'])
  })
})

describe('useMeetingRecording — typed-only meeting', () => {
  it('skips the upload and still resolves when nothing was recorded', async () => {
    const h = await mount()
    // No start() was ever called — a typed-only meeting.
    let uploaded: boolean | undefined
    await act(async () => { uploaded = await h.hook.result.current.stopAndUpload() })

    expect(uploaded).toBe(false)
    expect(h.upload).not.toHaveBeenCalled()
    expect(recorders).toHaveLength(0)
  })

  it('skips the upload when the recorder produced no chunks', async () => {
    const h = await mount()
    await startRecording(h)
    // Recorder ran but captured nothing (silence / immediate stop).
    let uploaded: boolean | undefined
    await act(async () => {
      const p = h.hook.result.current.stopAndUpload().then(v => { uploaded = v })
      await vi.advanceTimersByTimeAsync(1)
      await p
    })

    expect(uploaded).toBe(false)
    expect(h.upload).not.toHaveBeenCalled()
    expect(stoppedTracks).toEqual(['mic'])
  })
})

describe('useMeetingRecording — teardown', () => {
  it('releases the microphone on unmount', async () => {
    const h = await mount()
    await startRecording(h)
    h.hook.unmount()
    expect(stoppedTracks).toEqual(['mic'])
    expect(lastRecorder().state).toBe('inactive')
  })
})
